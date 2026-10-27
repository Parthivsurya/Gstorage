"""Which files currently have a public link, and how to withdraw one.

Sharing is not something the index can work out by looking: a piece being
world-readable is a fact on Drive, not in the local database, and withdrawing
a share needs the manifest's id, which exists nowhere else once the link has
been handed out. So it is recorded here.

One row per file. Re-sharing replaces the row rather than adding a second,
because two live manifests for one file means the older one can never be
withdrawn — nothing would remember it existed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShareRow:
    file_id: int
    manifest_file_id: str
    manifest_account_id: int | None
    link: str
    part_count: int
    shared_at: str


class Shares:
    def __init__(self, db) -> None:
        self.db = db

    def record(
        self,
        file_id: int,
        *,
        manifest_file_id: str,
        manifest_account_id: int | None,
        link: str,
        part_count: int,
    ) -> None:
        """Remember a share, replacing any earlier one for this file."""
        self.db.conn.execute(
            "INSERT INTO shares(file_id, manifest_file_id, manifest_account_id,"
            " link, part_count) VALUES(?,?,?,?,?) "
            "ON CONFLICT(file_id) DO UPDATE SET"
            " manifest_file_id = excluded.manifest_file_id,"
            " manifest_account_id = excluded.manifest_account_id,"
            " link = excluded.link,"
            " part_count = excluded.part_count,"
            " shared_at = datetime('now')",
            (file_id, manifest_file_id, manifest_account_id, link, part_count),
        )
        self.db.conn.commit()

    def get(self, file_id: int) -> ShareRow | None:
        row = self.db.conn.execute(
            "SELECT file_id, manifest_file_id, manifest_account_id, link,"
            " part_count, shared_at FROM shares WHERE file_id = ?",
            (file_id,),
        ).fetchone()
        return self._row(row) if row else None

    def clear(self, file_id: int) -> None:
        self.db.conn.execute("DELETE FROM shares WHERE file_id = ?", (file_id,))
        self.db.conn.commit()

    def shared_file_ids(self) -> set[int]:
        """Every file with a live link — for marking them in the listing."""
        rows = self.db.conn.execute("SELECT file_id FROM shares").fetchall()
        return {int(r["file_id"]) for r in rows}

    def list_all(self) -> list[ShareRow]:
        rows = self.db.conn.execute(
            "SELECT s.file_id, s.manifest_file_id, s.manifest_account_id, s.link,"
            " s.part_count, s.shared_at FROM shares s"
            " JOIN files f ON f.id = s.file_id"
            " ORDER BY s.shared_at DESC"
        ).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> ShareRow:
        return ShareRow(
            file_id=int(row["file_id"]),
            manifest_file_id=row["manifest_file_id"],
            manifest_account_id=(
                int(row["manifest_account_id"])
                if row["manifest_account_id"] is not None
                else None
            ),
            link=row["link"],
            part_count=int(row["part_count"]),
            shared_at=row["shared_at"],
        )
