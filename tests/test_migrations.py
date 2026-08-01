"""Tests for the SQLite index schema migrations (gstore.db.database).

The index is the only map from a virtual file to where its bytes physically
live, so a broken migration is a data-loss event. These tests verify that a
fresh database lands on the latest schema, that a legacy v1 database is upgraded
in-place through the whole migration chain without dropping existing rows, and
that initialize() is idempotent.

The v1 schema is reconstructed here deliberately (rather than imported) so the
migrations are exercised end to end against the real starting shape — the same
shape a user upgrading from the first release would have on disk.
"""

from __future__ import annotations

import sqlite3

from gstore.db.database import SCHEMA_VERSION, Database

# The original v1 on-disk schema, before any migration. Reconstructed from the
# _MIGRATIONS chain: no accounts.token_blob/status, no files.md5, no uploads or
# pending_chunks tables, and chunks carried an sha256 column (renamed to md5 at
# v6). A real v1 database was left unstamped (treated as version 1).
_V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE accounts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    email             TEXT NOT NULL UNIQUE,
    token_ref         TEXT,
    quota_limit_bytes INTEGER,
    quota_used_bytes  INTEGER,
    quota_synced_at   TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    parent_id  INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE files (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    parent_id       INTEGER REFERENCES folders(id) ON DELETE CASCADE,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    home_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    drive_file_id   TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    modified_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE chunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    sequence      INTEGER NOT NULL,
    account_id    INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    drive_file_id TEXT,
    sha256        TEXT,
    size_bytes    INTEGER NOT NULL DEFAULT 0,
    UNIQUE(file_id, sequence)
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }


def _write_v1_db(path) -> None:
    """Create a faithful, unstamped v1 database with one account + one file."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_V1_SCHEMA)
        conn.execute(
            "INSERT INTO accounts(id, email, quota_limit_bytes, quota_used_bytes, "
            "quota_synced_at) VALUES(1, 'legacy@example.com', 1000, 250, '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO files(id, name, parent_id, size_bytes, home_account_id, "
            "drive_file_id) VALUES(1, 'keep.txt', NULL, 42, 1, 'drive-abc')"
        )
        conn.commit()
    finally:
        conn.close()


# -- fresh install -----------------------------------------------------------


def test_fresh_db_is_at_latest_version(tmp_path):
    db = Database(tmp_path / "fresh.db").open().initialize()
    try:
        assert db.schema_version() == SCHEMA_VERSION
    finally:
        db.close()


def test_fresh_db_has_all_tables_and_columns(tmp_path):
    db = Database(tmp_path / "fresh.db").open().initialize()
    try:
        tables = _tables(db.conn)
        assert {
            "meta",
            "accounts",
            "folders",
            "files",
            "uploads",
            "pending_chunks",
            "chunks",
        } <= tables

        assert {"token_blob", "status"} <= _columns(db.conn, "accounts")
        assert "md5" in _columns(db.conn, "files")
        assert "md5" in _columns(db.conn, "chunks")
        assert "sha256" not in _columns(db.conn, "chunks")
    finally:
        db.close()


def test_initialize_is_idempotent(tmp_path):
    path = tmp_path / "fresh.db"
    Database(path).open().initialize().close()

    db = Database(path).open().initialize()  # second run must be a no-op
    try:
        assert db.schema_version() == SCHEMA_VERSION
    finally:
        db.close()


# -- legacy upgrade ----------------------------------------------------------


def test_v1_db_upgrades_to_latest_version(tmp_path):
    path = tmp_path / "legacy.db"
    _write_v1_db(path)

    db = Database(path).open().initialize()
    try:
        assert db.schema_version() == SCHEMA_VERSION
    finally:
        db.close()


def test_v1_upgrade_adds_new_tables_and_columns(tmp_path):
    path = tmp_path / "legacy.db"
    _write_v1_db(path)

    db = Database(path).open().initialize()
    try:
        assert {"uploads", "pending_chunks"} <= _tables(db.conn)
        assert {"token_blob", "status"} <= _columns(db.conn, "accounts")
        assert "md5" in _columns(db.conn, "files")
        # v6 renamed the chunk hash column sha256 -> md5.
        assert "md5" in _columns(db.conn, "chunks")
        assert "sha256" not in _columns(db.conn, "chunks")
    finally:
        db.close()


def test_v1_upgrade_preserves_existing_rows(tmp_path):
    path = tmp_path / "legacy.db"
    _write_v1_db(path)

    db = Database(path).open().initialize()
    try:
        account = db.conn.execute(
            "SELECT email, quota_used_bytes, status FROM accounts WHERE id = 1"
        ).fetchone()
        assert account["email"] == "legacy@example.com"
        assert account["quota_used_bytes"] == 250
        # New NOT NULL column backfilled with its default for existing rows.
        assert account["status"] == "active"

        file_row = db.conn.execute(
            "SELECT name, size_bytes, drive_file_id, md5 FROM files WHERE id = 1"
        ).fetchone()
        assert file_row["name"] == "keep.txt"
        assert file_row["size_bytes"] == 42
        assert file_row["drive_file_id"] == "drive-abc"
        assert file_row["md5"] is None  # column added empty; nothing to backfill
    finally:
        db.close()


def test_upgraded_db_accepts_chunk_inserts(tmp_path):
    """After upgrade the chunks table uses `md5` and takes Phase-B rows."""
    path = tmp_path / "legacy.db"
    _write_v1_db(path)

    db = Database(path).open().initialize()
    try:
        db.conn.execute(
            "INSERT INTO chunks(file_id, sequence, account_id, drive_file_id, "
            "md5, size_bytes) VALUES(1, 0, 1, 'part-0', 'abc123', 10)"
        )
        db.conn.commit()
        row = db.conn.execute(
            "SELECT md5, size_bytes FROM chunks WHERE file_id = 1 AND sequence = 0"
        ).fetchone()
        assert row["md5"] == "abc123"
        assert row["size_bytes"] == 10
    finally:
        db.close()
