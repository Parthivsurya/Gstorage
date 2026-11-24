"""Tests for the per-account Gstore folder (gstore.drive.folder).

Before this, every upload went to My Drive's root, so one chunked 20 GB file
dropped ~400 loose pieces across the top level of the user's own Drive.
Everything now goes inside one folder per account.

Two properties matter more than the happy path and have their own tests here:

* **A trashed folder is restored, never replaced.** Creating a replacement
  would leave every file already inside it in the user's trash, orphaned from
  an index still pointing at them — indistinguishable from total data loss
  while nothing has actually been lost.
* **Nothing here may ever fail a transfer.** This is housekeeping. Every
  failure degrades to "upload to the root", which is exactly what every
  release before this one did.

The Drive service is faked at :func:`gstore.drive.folder.drive_service`.
"""

from __future__ import annotations

import pytest
from googleapiclient.errors import HttpError

from gstore.auth.accounts import Account, AccountStore
from gstore.db.database import SCHEMA_VERSION, Database
from gstore.drive import folder as folder_mod
from gstore.drive.folder import (
    FOLDER_MIME,
    FOLDER_NAME,
    MISSING,
    OK,
    TRASHED,
    FolderResolver,
    create_folder,
    ensure_folder,
    folder_status,
)


def _http_error(status: int) -> HttpError:
    resp = type("Resp", (), {"status": status, "reason": "test"})()
    return HttpError(resp, b"{}")


class FakeDrive:
    """In-memory Drive holding {folder_id: trashed}, recording every call."""

    def __init__(self, folders: dict[str, bool] | None = None) -> None:
        self.folders: dict[str, bool] = dict(folders or {})
        self.created: list[dict] = []
        self.restored: list[str] = []
        self.gets = 0
        self._n = 0
        self.raise_on_create: Exception | None = None

    def service(self, credentials):  # signature of drive_service
        return _FakeService(self)


class _FakeFiles:
    def __init__(self, drive: FakeDrive) -> None:
        self._drive = drive

    def create(self, body=None, fields=None):
        drive = self._drive

        def execute():
            if drive.raise_on_create is not None:
                raise drive.raise_on_create
            drive.created.append(body)
            drive._n += 1
            new_id = f"folder{drive._n}"
            drive.folders[new_id] = False
            return {"id": new_id}

        return _FakeRequest(execute)

    def get(self, fileId=None, fields=None):
        drive = self._drive

        def execute():
            drive.gets += 1
            if fileId not in drive.folders:
                raise _http_error(404)
            return {"id": fileId, "trashed": drive.folders[fileId]}

        return _FakeRequest(execute)

    def update(self, fileId=None, body=None):
        drive = self._drive

        def execute():
            if body == {"trashed": False}:
                drive.folders[fileId] = False
                drive.restored.append(fileId)
            return {"id": fileId}

        return _FakeRequest(execute)


class _FakeService:
    def __init__(self, drive: FakeDrive) -> None:
        self._drive = drive

    def files(self):
        return _FakeFiles(self._drive)


class _FakeRequest:
    def __init__(self, execute) -> None:
        self.execute = execute


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(folder_mod, "drive_service", fake.service)
    return fake


# -- creating ----------------------------------------------------------------


def test_create_sends_the_folder_mime_type(drive):
    folder_id = create_folder(None)

    assert drive.created == [{"name": FOLDER_NAME, "mimeType": FOLDER_MIME}]
    assert folder_id == "folder1"


def test_created_folder_has_no_parent_so_it_sits_at_the_drive_root(drive):
    """The folder itself is top level; only its contents move out of the root."""
    create_folder(None)

    assert "parents" not in drive.created[0]


# -- reading state -----------------------------------------------------------


def test_status_of_a_live_folder_is_ok(drive):
    drive.folders["f1"] = False
    assert folder_status(None, "f1") == OK


def test_status_of_a_trashed_folder_is_trashed(drive):
    drive.folders["f1"] = True
    assert folder_status(None, "f1") == TRASHED


def test_status_of_a_deleted_folder_is_missing(drive):
    assert folder_status(None, "gone") == MISSING


def test_a_404_is_not_retried_into_oblivion(drive):
    """404 is absent from the retryable set, so this returns rather than looping."""
    assert folder_status(None, "gone") == MISSING
    assert drive.gets == 1


# -- ensure_folder: the decision table ---------------------------------------


def test_with_nothing_remembered_a_folder_is_created(drive):
    assert ensure_folder(None, None) == "folder1"
    assert len(drive.created) == 1


def test_a_healthy_folder_is_reused_and_nothing_is_created(drive):
    drive.folders["f1"] = False

    assert ensure_folder(None, "f1") == "f1"
    assert drive.created == []


def test_a_trashed_folder_is_restored_not_replaced(drive):
    """The important one: a replacement would orphan everything inside it."""
    drive.folders["f1"] = True

    assert ensure_folder(None, "f1") == "f1"
    assert drive.restored == ["f1"]
    assert drive.created == [], "a replacement folder would strand the old files"
    assert drive.folders["f1"] is False


def test_a_permanently_deleted_folder_is_replaced(drive):
    result = ensure_folder(None, "gone")

    assert result == "folder1"
    assert len(drive.created) == 1


def test_an_unexpected_failure_degrades_to_the_root_rather_than_raising(drive):
    drive.raise_on_create = RuntimeError("Drive is having a day")

    assert ensure_folder(None, None) is None


def test_a_failure_confirming_the_folder_also_degrades_to_the_root(
    drive, monkeypatch
):
    def explode(credentials, folder_id):
        raise HttpError(type("R", (), {"status": 500, "reason": "x"})(), b"{}")

    monkeypatch.setattr(folder_mod, "folder_status", explode)
    assert ensure_folder(None, "f1") is None


# -- FolderResolver ----------------------------------------------------------


class FakeAccounts:
    """Just the three AccountStore methods the resolver touches."""

    def __init__(self, stored: dict[int, str | None] | None = None) -> None:
        self.stored: dict[int, str | None] = dict(stored or {})
        self.writes: list[tuple[int, str | None]] = []
        self.credential_calls = 0
        self.raise_credentials: Exception | None = None

    def drive_folder_id(self, account_id):
        return self.stored.get(account_id)

    def set_drive_folder_id(self, account_id, folder_id):
        self.stored[account_id] = folder_id
        self.writes.append((account_id, folder_id))

    def get_credentials(self, account_id):
        self.credential_calls += 1
        if self.raise_credentials is not None:
            raise self.raise_credentials
        return f"creds{account_id}"


def test_resolver_confirms_each_account_once_however_many_chunks(drive):
    """The performance guarantee: 400 chunks must not mean 400 round trips."""
    drive.folders["f1"] = False
    accounts = FakeAccounts({7: "f1"})
    resolver = FolderResolver(accounts)

    for _ in range(400):
        assert resolver.folder_id(7) == "f1"

    assert drive.gets == 1
    assert accounts.credential_calls == 1


def test_resolver_keeps_each_account_separate(drive):
    drive.folders.update({"f1": False, "f2": False})
    resolver = FolderResolver(FakeAccounts({1: "f1", 2: "f2"}))

    assert resolver.folder_id(1) == "f1"
    assert resolver.folder_id(2) == "f2"


def test_a_newly_created_folder_is_remembered_for_next_launch(drive):
    accounts = FakeAccounts({3: None})
    resolver = FolderResolver(accounts)

    assert resolver.folder_id(3) == "folder1"
    assert accounts.writes == [(3, "folder1")]


def test_an_unchanged_folder_is_not_rewritten(drive):
    drive.folders["f1"] = False
    accounts = FakeAccounts({3: "f1"})

    FolderResolver(accounts).folder_id(3)

    assert accounts.writes == []


def test_unreachable_credentials_give_the_root_not_an_exception(drive):
    """A revoked account fails later, with its own message — not here."""
    accounts = FakeAccounts({4: None})
    accounts.raise_credentials = RuntimeError("revoked")
    resolver = FolderResolver(accounts)

    assert resolver.folder_id(4) is None


def test_a_failure_is_cached_too_so_it_is_not_retried_per_chunk(drive):
    accounts = FakeAccounts({4: None})
    accounts.raise_credentials = RuntimeError("revoked")
    resolver = FolderResolver(accounts)

    for _ in range(50):
        resolver.folder_id(4)

    assert accounts.credential_calls == 1


# -- persistence -------------------------------------------------------------


def test_the_column_round_trips_through_the_real_store(tmp_path):
    db = Database(tmp_path / "idx.db").open().initialize()
    try:
        db.conn.execute("INSERT INTO accounts(email) VALUES('a@example.com')")
        db.conn.commit()
        store = AccountStore(db)
        account_id = db.conn.execute("SELECT id FROM accounts").fetchone()["id"]

        assert store.drive_folder_id(account_id) is None

        store.set_drive_folder_id(account_id, "abc123")
        assert store.drive_folder_id(account_id) == "abc123"
        assert store.list_accounts()[0].drive_folder_id == "abc123"
    finally:
        db.close()


def test_folder_id_of_an_unknown_account_is_none_not_an_error(tmp_path):
    db = Database(tmp_path / "idx.db").open().initialize()
    try:
        assert AccountStore(db).drive_folder_id(999) is None
    finally:
        db.close()


def test_account_still_builds_without_a_folder_id():
    """The field is defaulted, so every existing construction keeps working."""
    account = Account(
        id=1,
        email="a@example.com",
        quota_limit_bytes=None,
        quota_used_bytes=None,
        quota_synced_at=None,
        status="active",
        created_at="2026-01-01T00:00:00",
    )
    assert account.drive_folder_id is None


# -- migration ---------------------------------------------------------------

_V10_ACCOUNTS = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE accounts (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    email              TEXT NOT NULL UNIQUE,
    token_ref          TEXT,
    token_blob         BLOB,
    quota_limit_bytes  INTEGER,
    quota_used_bytes   INTEGER,
    quota_synced_at    TEXT,
    status             TEXT NOT NULL DEFAULT 'active',
    created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO accounts(email) VALUES('existing@example.com');
INSERT INTO meta(key, value) VALUES('schema_version', '10');
"""


def test_a_v10_database_gains_the_column_and_keeps_its_accounts(tmp_path):
    """The path a real 0.1.3 user takes on upgrade."""
    import sqlite3

    path = tmp_path / "idx.db"
    raw = sqlite3.connect(path)
    raw.executescript(_V10_ACCOUNTS)
    raw.commit()
    raw.close()

    db = Database(path).open().initialize()
    try:
        assert db.schema_version() == SCHEMA_VERSION
        rows = db.conn.execute(
            "SELECT email, drive_folder_id FROM accounts"
        ).fetchall()
        assert [r["email"] for r in rows] == ["existing@example.com"]
        assert rows[0]["drive_folder_id"] is None, "nothing to place it in yet"
    finally:
        db.close()


# -- the parent actually reaches Drive ---------------------------------------


class _RecordingFiles:
    """Captures the create body of a resumable upload and completes it."""

    def __init__(self, bodies: list[dict]) -> None:
        self._bodies = bodies

    def create(self, body=None, media_body=None, fields=None):
        self._bodies.append(body)
        return _OneShotUpload()

    # The small-file path reaches files().create the same way; nothing else
    # of the service is touched.


class _OneShotUpload:
    """Answers both shapes: resumable next_chunk and the one-request execute."""

    resumable_uri = "https://upload.example/session"
    _RESPONSE = {"id": "uploaded1", "size": "3", "md5Checksum": None}

    def next_chunk(self):
        return None, dict(self._RESPONSE, md5Checksum=_ABC_MD5)

    def execute(self):
        return dict(self._RESPONSE, md5Checksum=_ABC_MD5)


_ABC_MD5 = "900150983cd24fb0d6963f7d28e17f72"  # md5(b"abc")


def test_a_whole_file_upload_is_placed_inside_the_folder(tmp_path, monkeypatch):
    from gstore.drive import upload as upload_mod

    path = tmp_path / "f.bin"
    path.write_bytes(b"abc")
    bodies: list[dict] = []
    monkeypatch.setattr(
        upload_mod,
        "drive_service",
        lambda creds: type("S", (), {"files": lambda self: _RecordingFiles(bodies)})(),
    )

    upload_mod.upload_file(None, path, parent_id="folder9")

    assert bodies[0]["parents"] == ["folder9"]


def test_a_whole_file_upload_without_a_folder_still_goes_to_the_root(
    tmp_path, monkeypatch
):
    """Back-compat: the pre-0.1.4 behaviour, and the fallback when Drive balks."""
    from gstore.drive import upload as upload_mod

    path = tmp_path / "f.bin"
    path.write_bytes(b"abc")
    bodies: list[dict] = []
    monkeypatch.setattr(
        upload_mod,
        "drive_service",
        lambda creds: type("S", (), {"files": lambda self: _RecordingFiles(bodies)})(),
    )

    upload_mod.upload_file(None, path, parent_id=None)

    assert "parents" not in bodies[0]


# -- reconcile ---------------------------------------------------------------


def test_repair_does_not_offer_the_folder_back_as_a_lost_file(monkeypatch):
    """The folder is app-created, so it appears in files.list unless excluded."""
    from gstore.drive import reconcile as reconcile_mod

    queries: list[str] = []

    class Files:
        def list(self, q=None, spaces=None, fields=None, pageSize=None, pageToken=None):
            queries.append(q)
            return _FakeRequest(lambda: {"files": []})

    monkeypatch.setattr(
        reconcile_mod,
        "drive_service",
        lambda creds: type("S", (), {"files": lambda self: Files()})(),
    )

    reconcile_mod.list_app_files(None)

    assert f"mimeType != '{FOLDER_MIME}'" in queries[0]
    assert "trashed=false" in queries[0]
