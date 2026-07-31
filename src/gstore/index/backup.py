"""Back up and restore the local index.

The SQLite index is the only record of what lives where across the pooled
accounts — lose it and the map to your files is gone, even though the files
themselves still sit in Drive. These helpers let the user snapshot the index to
a single portable file and restore it later.

Both directions use SQLite's online backup API, which produces a consistent copy
even with WAL mode and a live connection. Restore copies *into* the running
connection, so there's no file swap or reopen: existing stores keep working and
the UI just re-reads.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from gstore.db.database import SCHEMA_VERSION

# Tables a genuine Gstore index must contain — a sanity check before restoring.
_REQUIRED_TABLES = {"meta", "accounts", "folders", "files"}


class InvalidBackup(ValueError):
    """Raised when a file chosen for restore isn't a usable Gstore index."""


def export_index(db, dest_path: str | Path) -> Path:
    """Write a consistent snapshot of the index to `dest_path`."""
    dest = sqlite3.connect(str(dest_path))
    try:
        db.conn.backup(dest)
    finally:
        dest.close()
    return Path(dest_path)


def import_index(db, src_path: str | Path) -> None:
    """Replace the current index with the backup at `src_path`.

    Validates the file first, copies it over the live connection, then applies
    any pending migrations so an older backup is brought up to the current
    schema. Raises :class:`InvalidBackup` if the file isn't a Gstore index or
    was written by a newer version.
    """
    src = sqlite3.connect(f"file:{Path(src_path)}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        _validate(src)
        src.backup(db.conn)
    finally:
        src.close()
    # Restored data may predate the running schema; migrate it forward.
    db.initialize()


def _validate(src: sqlite3.Connection) -> None:
    tables = {
        r["name"]
        for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = _REQUIRED_TABLES - tables
    if missing:
        raise InvalidBackup(
            "This file is not a Gstore index backup "
            f"(missing tables: {', '.join(sorted(missing))})."
        )

    row = src.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    version = int(row["value"]) if row else 1
    if version > SCHEMA_VERSION:
        raise InvalidBackup(
            f"This backup was made by a newer version of Gstore "
            f"(index v{version} > supported v{SCHEMA_VERSION}). Update Gstore first."
        )
