"""Persistence for in-flight chunked downloads (chunk-level resume).

The upload side has `pending_chunks`; this is its counterpart. It exists
because parallel download workers finish out of order, so the output file's
length stops being evidence of progress — it is preallocated to the full size
and filled in at offsets. Which chunks actually landed has to be recorded.

Keyed to the destination path together with the file's identity (its index id
and total size), so aiming a different download at the same path does not
inherit the previous one's progress.
"""

from __future__ import annotations


class DownloadProgress:
    def __init__(self, db) -> None:
        self.db = db

    def reset_if_changed(self, dest_path: str, file_id: int, total_bytes: int) -> None:
        """Drop progress recorded for a different file at this destination."""
        self.db.conn.execute(
            "DELETE FROM download_progress WHERE dest_path = ? "
            "AND (file_id != ? OR total_bytes != ?)",
            (dest_path, file_id, total_bytes),
        )
        self.db.conn.commit()

    def completed(self, dest_path: str, file_id: int, total_bytes: int) -> set[int]:
        """Sequences already written into the output file for this exact file."""
        rows = self.db.conn.execute(
            "SELECT sequence FROM download_progress WHERE dest_path = ? "
            "AND file_id = ? AND total_bytes = ?",
            (dest_path, file_id, total_bytes),
        ).fetchall()
        return {row["sequence"] for row in rows}

    def record(
        self,
        dest_path: str,
        file_id: int,
        total_bytes: int,
        sequence: int,
        size_bytes: int,
    ) -> None:
        """Mark one chunk written. Only called after its checksum verified."""
        self.db.conn.execute(
            "INSERT INTO download_progress(dest_path, file_id, total_bytes, "
            "sequence, size_bytes) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(dest_path, sequence) DO UPDATE SET "
            "file_id = excluded.file_id, total_bytes = excluded.total_bytes, "
            "size_bytes = excluded.size_bytes",
            (dest_path, file_id, total_bytes, sequence, size_bytes),
        )
        self.db.conn.commit()

    def done_bytes(self, dest_path: str, file_id: int, total_bytes: int) -> int:
        """How much of the output is already present."""
        row = self.db.conn.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) AS n FROM download_progress "
            "WHERE dest_path = ? AND file_id = ? AND total_bytes = ?",
            (dest_path, file_id, total_bytes),
        ).fetchone()
        return int(row["n"])

    def clear(self, dest_path: str) -> None:
        self.db.conn.execute(
            "DELETE FROM download_progress WHERE dest_path = ?", (dest_path,)
        )
        self.db.conn.commit()
