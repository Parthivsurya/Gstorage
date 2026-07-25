"""SQLite connection management and schema bootstrap.

The database is created lazily under the app-data directory the first time it is
opened. `initialize()` is idempotent — safe to call on every launch.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from gstore.config import database_path

SCHEMA_VERSION = 1

_SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


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
        """Create tables if missing and stamp the schema version."""
        if self._conn is None:
            self.open()
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()
        return self

    def schema_version(self) -> int | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Database":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()
