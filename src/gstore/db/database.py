"""SQLite connection management and schema bootstrap.

The database is created lazily under the app-data directory the first time it is
opened. `initialize()` is idempotent — safe to call on every launch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from gstore.config import database_path

SCHEMA_VERSION = 10

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# Incremental migrations applied to databases created by an older version.
# A fresh install already gets the latest schema from schema.sql, so these only
# run when upgrading an existing DB. Keyed by the version they bring the DB to.
_MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE accounts ADD COLUMN token_blob BLOB"],
    3: ["ALTER TABLE files ADD COLUMN md5 TEXT"],
    4: [
        "CREATE TABLE IF NOT EXISTS uploads ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " account_id INTEGER REFERENCES accounts(id) ON DELETE CASCADE,"
        " local_path TEXT NOT NULL UNIQUE,"
        " size_bytes INTEGER NOT NULL,"
        " mtime_ns INTEGER NOT NULL,"
        " resumable_uri TEXT NOT NULL,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    ],
    5: ["ALTER TABLE accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"],
    # chunks is empty in every pre-Phase-B DB (reserved, unused), so renaming
    # the hash column to match the rest of the codebase is safe.
    6: ["ALTER TABLE chunks RENAME COLUMN sha256 TO md5"],
    7: [
        "CREATE TABLE IF NOT EXISTS pending_chunks ("
        " local_path TEXT NOT NULL,"
        " size_bytes INTEGER NOT NULL,"
        " mtime_ns INTEGER NOT NULL,"
        " sequence INTEGER NOT NULL,"
        " account_id INTEGER,"
        " drive_file_id TEXT NOT NULL,"
        " md5 TEXT NOT NULL,"
        " chunk_size INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (local_path, sequence))"
    ],
    8: ["ALTER TABLE pending_chunks ADD COLUMN file_md5 TEXT"],
    10: [
        "CREATE TABLE IF NOT EXISTS shares ("
        " file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,"
        " manifest_file_id TEXT NOT NULL,"
        " manifest_account_id INTEGER REFERENCES accounts(id) ON DELETE SET NULL,"
        " link TEXT NOT NULL,"
        " part_count INTEGER NOT NULL DEFAULT 0,"
        " shared_at TEXT NOT NULL DEFAULT (datetime('now')))"
    ],
    9: [
        "CREATE TABLE IF NOT EXISTS download_progress ("
        " dest_path TEXT NOT NULL,"
        " file_id INTEGER NOT NULL,"
        " total_bytes INTEGER NOT NULL,"
        " sequence INTEGER NOT NULL,"
        " size_bytes INTEGER NOT NULL,"
        " PRIMARY KEY (dest_path, sequence))",
        "CREATE INDEX IF NOT EXISTS idx_download_progress_dest"
        " ON download_progress(dest_path, file_id, total_bytes)",
    ],
}


class Database:
    """Thin wrapper over a SQLite connection to the local index."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or database_path()
        self._conn: sqlite3.Connection | None = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open; call open() first.")
        return self._conn

    def open(self) -> "Database":
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.execute("PRAGMA journal_mode = WAL;")
        return self

    def initialize(self) -> "Database":
        """Create tables if missing, apply pending migrations, stamp version.

        Idempotent. A brand-new DB gets the full latest schema from schema.sql;
        an existing older DB is upgraded incrementally via _MIGRATIONS.
        """
        if self._conn is None:
            self.open()

        had_accounts = self._table_exists("accounts")
        prior_version = self._read_version()

        # Fresh installs receive the latest schema here; CREATE IF NOT EXISTS
        # makes this a no-op for tables that already exist.
        self.conn.executescript(_SCHEMA_SQL)

        # A DB with no prior accounts table is brand-new -> already at latest.
        # Otherwise upgrade from its recorded version (unstamped legacy == 1).
        start = SCHEMA_VERSION if not had_accounts else (prior_version or 1)
        for version in range(start + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS.get(version, []):
                try:
                    self.conn.execute(statement)
                except sqlite3.OperationalError as err:
                    # schema.sql above already creates, in full, any table
                    # introduced after this DB's recorded version — so an
                    # ADD COLUMN migration for one of those finds the column
                    # present. That is redundant, not broken: an upgrade from
                    # v1 would otherwise fail outright on a column that a
                    # later migration's own CREATE TABLE had just supplied.
                    if "duplicate column name" not in str(err):
                        raise

        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()
        return self

    def _table_exists(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def _read_version(self) -> int | None:
        if not self._table_exists("meta"):
            return None
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else None

    def schema_version(self) -> int | None:
        return self._read_version()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()
