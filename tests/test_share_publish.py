"""Tests for granting and withdrawing the public permission.

Sharing a 20 GB file is 409 separate permission changes across several
accounts, and a partial result is the dangerous outcome in both directions: a
share missing one piece is unopenable, and an un-share that missed one leaves
that piece world-readable forever. So these check that every piece is
covered, that the right account's credentials are used for each, and that
un-sharing keeps going over pieces that have already gone.

The Drive service is faked at :func:`gstore.share.publish.drive_service`.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from gstore.share import publish as publish_mod
from gstore.share.manifest import Manifest, decode_link
from gstore.share.publish import NotShareable, share_file, unshare_file


class FakeHttpError(Exception):
    def __init__(self, status):
        self.resp = type("R", (), {"status": status})()


@dataclass
class FakeChunk:
    sequence: int
    account_id: int | None
    drive_file_id: str | None
    md5: str | None
    size_bytes: int


@dataclass
class FakeFile:
    id: int
    name: str
    size_bytes: int
    md5: str | None
    drive_file_id: str | None = None
    home_account_id: int | None = None
    chunk_count: int = 0

    @property
    def is_chunked(self):
        return self.chunk_count > 0


class FakeIndex:
    def __init__(self, chunks=()):
        self._chunks = list(chunks)

    def list_chunks(self, file_id):
        return list(self._chunks)


class FakeDrive:
    """Records permission changes and uploads, per account."""

    def __init__(self):
        self.public: set[str] = set()
        self.uploaded: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.used_accounts: list[int] = []
        self.missing: set[str] = set()
        self._n = 0

    def service_for(self, account_id):
        self.used_accounts.append(account_id)
        return _FakeService(self, account_id)


class _FakeService:
    def __init__(self, drive, account_id):
        self._d, self._a = drive, account_id

    def permissions(self):
        return _FakePermissions(self._d)

    def files(self):
        return _FakeFiles(self._d)


class _Req:
    def __init__(self, fn):
        self.execute = fn


class _FakePermissions:
    def __init__(self, drive):
        self._d = drive

    def create(self, fileId, body, fields=None):
        def run():
            if fileId in self._d.missing:
                raise FakeHttpError(404)
            self._d.public.add(fileId)
            return {"id": "perm"}
        return _Req(run)

    def list(self, fileId, fields=None):
        def run():
            if fileId in self._d.missing:
                raise FakeHttpError(404)
            found = [{"id": "perm", "type": "anyone"}] if fileId in self._d.public else []
            return {"permissions": found}
        return _Req(run)

    def delete(self, fileId, permissionId):
        def run():
            self._d.public.discard(fileId)
            return {}
        return _Req(run)


class _FakeFiles:
    def __init__(self, drive):
        self._d = drive

    def create(self, body, media_body, fields=None):
        def run():
            self._d._n += 1
            file_id = f"manifest-{self._d._n}"
            media_body._fd.seek(0)
            self._d.uploaded[file_id] = media_body._fd.read()
            return {"id": file_id}
        return _Req(run)

    def delete(self, fileId):
        def run():
            self._d.deleted.append(fileId)
            self._d.uploaded.pop(fileId, None)
            return {}
        return _Req(run)


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(publish_mod, "drive_service", lambda creds, **kw: fake.service_for(creds))
    monkeypatch.setattr(publish_mod, "HttpError", FakeHttpError)
    return fake


def _credentials_for(account_id):
    return account_id            # the fake service keys off this directly


def _split_file(pieces=5):
    chunks = [
        FakeChunk(i, account_id=(i % 2) + 1, drive_file_id=f"piece-{i}",
                  md5=f"{i:032d}", size_bytes=100)
        for i in range(pieces)
    ]
    row = FakeFile(id=1, name="big.rar", size_bytes=100 * pieces, md5="f" * 32,
                   chunk_count=pieces)
    return FakeIndex(chunks), row


# -- sharing ---------------------------------------------------------------


def test_every_piece_of_a_split_file_is_made_public(drive):
    index, row = _split_file(pieces=5)
    result = share_file(index, row, _credentials_for)
    assert {f"piece-{i}" for i in range(5)} <= drive.public
    assert result.part_count == 5


def test_the_manifest_itself_is_made_public_too(drive):
    # Otherwise the recipient cannot read the thing the link points at.
    index, row = _split_file()
    result = share_file(index, row, _credentials_for)
    assert result.manifest_file_id in drive.public


def test_each_piece_is_shared_with_its_own_account(drive):
    # Pieces span accounts; using one account's credentials for all of them
    # would 404 on everything it does not own.
    index, row = _split_file(pieces=6)
    share_file(index, row, _credentials_for)
    assert {1, 2} <= set(drive.used_accounts)


def test_the_link_opens_the_manifest_that_was_uploaded(drive):
    index, row = _split_file(pieces=4)
    result = share_file(index, row, _credentials_for)

    manifest_id, key = decode_link(result.link)
    assert manifest_id == result.manifest_file_id

    manifest = Manifest.unseal(drive.uploaded[manifest_id], key)
    assert manifest.name == "big.rar"
    assert [p.drive_file_id for p in manifest.parts] == [f"piece-{i}" for i in range(4)]


def test_a_whole_file_share_needs_no_chunks(drive):
    row = FakeFile(id=2, name="small.pdf", size_bytes=900, md5="a" * 32,
                   drive_file_id="whole-1", home_account_id=7)
    result = share_file(FakeIndex(), row, _credentials_for)
    assert "whole-1" in drive.public
    assert result.part_count == 1


def test_progress_is_reported_for_every_piece(drive):
    index, row = _split_file(pieces=5)
    seen = []
    share_file(index, row, _credentials_for, on_progress=lambda d, t: seen.append((d, t)))
    assert seen[-1] == (5, 5)


def test_a_file_still_uploading_is_refused_before_anything_is_made_public(drive):
    row = FakeFile(id=3, name="part.bin", size_bytes=10, md5=None)
    with pytest.raises(NotShareable, match="not finished uploading"):
        share_file(FakeIndex(), row, _credentials_for)
    assert not drive.public


def test_a_file_with_an_unreachable_piece_is_refused_with_a_count(drive):
    # Half-sharing it would produce a link that cannot rebuild the file.
    chunks = [
        FakeChunk(0, 1, "piece-0", "0" * 32, 100),
        FakeChunk(1, None, None, None, 100),      # account was unlinked
    ]
    row = FakeFile(id=4, name="big.rar", size_bytes=200, md5="f" * 32, chunk_count=2)
    with pytest.raises(NotShareable, match="1 of this file's 2 pieces"):
        share_file(FakeIndex(chunks), row, _credentials_for)
    assert not drive.public


# -- withdrawing -----------------------------------------------------------


def test_unsharing_makes_every_piece_private_again(drive):
    index, row = _split_file(pieces=5)
    result = share_file(index, row, _credentials_for)
    unshare_file(index, row, _credentials_for,
                 manifest_file_id=result.manifest_file_id,
                 manifest_account_id=result.manifest_account_id)
    assert not {f"piece-{i}" for i in range(5)} & drive.public


def test_unsharing_deletes_the_manifest(drive):
    index, row = _split_file()
    result = share_file(index, row, _credentials_for)
    unshare_file(index, row, _credentials_for,
                 manifest_file_id=result.manifest_file_id,
                 manifest_account_id=result.manifest_account_id)
    assert result.manifest_file_id in drive.deleted


def test_unsharing_continues_past_pieces_that_are_already_gone(drive):
    # A piece deleted from Drive by hand must not strand the rest as public.
    index, row = _split_file(pieces=4)
    share_file(index, row, _credentials_for)
    drive.missing.add("piece-2")
    unshare_file(index, row, _credentials_for)
    assert not {"piece-0", "piece-1", "piece-3"} & drive.public


def test_unsharing_a_file_that_was_never_shared_is_harmless(drive):
    index, row = _split_file()
    unshare_file(index, row, _credentials_for)
    assert not drive.public
