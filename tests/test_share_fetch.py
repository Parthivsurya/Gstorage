"""Tests for reading a shared file back without any credentials.

This path has no OAuth, no googleapiclient and no retry wrapper underneath
it — just HTTP — so the checks it does itself are the only ones there are.
The important ones are about *bad* responses: an unauthenticated Drive
endpoint answers trouble with an HTML page and a 200, so "50 MB of HTML" is
a real thing that can land in the middle of a file.

The network is faked at :func:`gstore.share.fetch._open`, the one seam, with
an in-memory {file_id: bytes} store.
"""

from __future__ import annotations

import hashlib
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from gstore.share import fetch as fetch_mod
from gstore.share.fetch import PieceCorrupt, ShareUnavailable, download_shared
from gstore.share.manifest import Manifest, Part


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class FakeResponse:
    def __init__(self, payload: bytes):
        self._payload, self._pos = payload, 0

    def read(self, size=None):
        if size is None:
            block, self._pos = self._payload[self._pos:], len(self._payload)
            return block
        block = self._payload[self._pos : self._pos + size]
        self._pos += len(block)
        return block

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDrive:
    """Serves public files by id, and can be told to misbehave."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.status_for: dict[str, int] = {}
        self.requested: list[str] = []

    def add(self, file_id: str, payload: bytes) -> Part:
        self.store[file_id] = payload
        return Part(file_id, _md5(payload), len(payload))

    def open(self, url: str, *_, **__):
        file_id = parse_qs(urlsplit(url).query)["id"][0]
        self.requested.append(file_id)
        if file_id in self.status_for:
            raise urllib.error.HTTPError(url, self.status_for[file_id], "no", {}, None)
        if file_id not in self.store:
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        return FakeResponse(self.store[file_id])


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(fetch_mod, "_open", fake.open)
    return fake


def _manifest(drive, pieces, *, name="shared.bin"):
    parts = [drive.add(f"id-{i}", payload) for i, payload in enumerate(pieces)]
    whole = b"".join(pieces)
    return Manifest(name, len(whole), _md5(whole), tuple(parts)), whole


# -- the happy path --------------------------------------------------------


def test_rebuilds_a_split_file_byte_for_byte(drive, tmp_path):
    manifest, whole = _manifest(drive, [b"A" * 4096, b"B" * 4096, b"C" * 1234])
    dest = tmp_path / "out.bin"
    download_shared(manifest, dest, streams=3)
    assert dest.read_bytes() == whole


def test_pieces_land_in_the_right_order_however_they_arrive(drive, tmp_path):
    # Several streams finish out of order; offsets, not arrival, decide layout.
    pieces = [bytes([i]) * 2048 for i in range(8)]
    manifest, whole = _manifest(drive, pieces)
    dest = tmp_path / "out.bin"
    download_shared(manifest, dest, streams=8)
    assert dest.read_bytes() == whole


def test_a_whole_file_share_is_just_one_piece(drive, tmp_path):
    manifest, whole = _manifest(drive, [b"only" * 1000])
    dest = tmp_path / "out.bin"
    download_shared(manifest, dest)
    assert dest.read_bytes() == whole


def test_progress_reaches_the_full_size(drive, tmp_path):
    manifest, whole = _manifest(drive, [b"x" * 1000, b"y" * 1000])
    seen = []
    download_shared(manifest, tmp_path / "out.bin", on_progress=lambda d, t: seen.append((d, t)))
    assert seen[-1] == (len(whole), len(whole))


def test_nothing_is_fetched_with_credentials(drive, tmp_path):
    # The point of the whole feature: the recipient has no account.
    manifest, _ = _manifest(drive, [b"z" * 512])
    download_shared(manifest, tmp_path / "out.bin")
    assert drive.requested == ["id-0"]


# -- the failure paths that matter ----------------------------------------


def test_a_piece_that_does_not_match_its_checksum_is_refused(drive, tmp_path):
    manifest, _ = _manifest(drive, [b"A" * 1024, b"B" * 1024])
    drive.store["id-1"] = b"C" * 1024          # right length, wrong bytes
    with pytest.raises(PieceCorrupt, match="checksum"):
        download_shared(manifest, tmp_path / "out.bin")


def test_a_short_piece_is_refused(drive, tmp_path):
    manifest, _ = _manifest(drive, [b"A" * 1024])
    drive.store["id-0"] = b"A" * 500
    with pytest.raises(PieceCorrupt, match="bytes long"):
        download_shared(manifest, tmp_path / "out.bin")


def test_an_html_page_served_instead_of_bytes_is_caught(drive, tmp_path):
    # Drive answers a withdrawn or rate-limited share with a 200 and a page.
    manifest, _ = _manifest(drive, [b"A" * 1024])
    drive.store["id-0"] = b"<!doctype html><html>Quota exceeded</html>"
    with pytest.raises(ShareUnavailable, match="web page"):
        download_shared(manifest, tmp_path / "out.bin")


def test_a_withdrawn_share_says_so_rather_than_showing_a_status_code(drive, tmp_path):
    manifest, _ = _manifest(drive, [b"A" * 1024])
    drive.status_for["id-0"] = 404
    with pytest.raises(ShareUnavailable, match="no longer available"):
        download_shared(manifest, tmp_path / "out.bin")


def test_a_rebuilt_file_that_fails_the_whole_file_check_is_refused(drive, tmp_path):
    # Every piece passes its own checksum, but the manifest's total is wrong:
    # the one case per-piece hashes cannot catch.
    manifest, _ = _manifest(drive, [b"A" * 1024, b"B" * 1024])
    manifest = Manifest(manifest.name, manifest.size_bytes, "0" * 32, manifest.parts)
    with pytest.raises(PieceCorrupt, match="sender's checksum"):
        download_shared(manifest, tmp_path / "out.bin")


@pytest.mark.parametrize(
    "break_it",
    [
        lambda d: d.store.__setitem__("id-1", b"C" * 1024),
        lambda d: d.status_for.__setitem__("id-1", 403),
    ],
    ids=["corrupt-piece", "piece-unavailable"],
)
def test_the_destination_never_appears_unless_everything_verified(drive, tmp_path, break_it):
    manifest, _ = _manifest(drive, [b"A" * 1024, b"B" * 1024])
    break_it(drive)
    dest = tmp_path / "out.bin"
    with pytest.raises(ShareUnavailable):
        download_shared(manifest, dest)
    assert not dest.exists(), "a failed share must not leave a half-written file"


def test_cancelling_leaves_the_destination_alone(drive, tmp_path):
    manifest, _ = _manifest(drive, [b"A" * 1024, b"B" * 1024])
    dest = tmp_path / "out.bin"
    with pytest.raises(ShareUnavailable, match="cancelled"):
        download_shared(manifest, dest, should_stop=lambda: True)
    assert not dest.exists()


# -- configuration ---------------------------------------------------------


def test_stream_count_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv(fetch_mod.STREAMS_ENV, raising=False)
    assert fetch_mod.stream_count() == fetch_mod.DEFAULT_STREAMS
    for raw, expected in [("1", 1), ("8", 8), ("999", fetch_mod.MAX_STREAMS), ("0", 1)]:
        monkeypatch.setenv(fetch_mod.STREAMS_ENV, raw)
        assert fetch_mod.stream_count() == expected


def test_a_nonsense_stream_count_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setenv(fetch_mod.STREAMS_ENV, "lots")
    assert fetch_mod.stream_count() == fetch_mod.DEFAULT_STREAMS
