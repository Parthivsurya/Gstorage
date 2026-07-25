"""SQLite connection management and schema bootstrap.

The database is created lazily under the app-data directory the first time it is
opened. `initialize()` is idempotent — safe to call on every launch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from gstore.config import database_path

SCHEMA_VERSION = 2

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# Incremental migrations applied to databases created by an older version.
# A fresh install already gets the latest schema from schema.sql, so these only
# run when upgrading an existing DB. Keyed by the version they bring the DB to.
_MIGRATIONS: dict[int, list[str]] = {
    2: ["ALTER TABLE accounts ADD COLUMN token_blob BLOB"],
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
                self.conn.execute(statement)

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
