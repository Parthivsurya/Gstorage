"""Persistence for in-flight chunked uploads (chunk-level resume).

Records each chunk of a chunked upload as it completes so an interrupted upload
(dropped connection, crash, quit) resumes from the first missing chunk instead
of re-uploading everything. Rows are keyed to the local file's identity (path +
size + mtime) and cleared once the whole upload finishes.

Kept database-only (returns plain rows) so the drive layer stays DB-agnostic;
the worker adapts these rows to the resume interface upload_chunks expects.
"""

from __future__ import annotations

import sqlite3


class PendingChunks:
    def __init__(self, db) -> None:
        self.db = db

    def reset_if_changed(self, local_path: str, size_bytes: int, mtime_ns: int) -> None:
        """Drop staged chunks for a path if the file changed since they were made.

        Their Drive files become orphans, which reconcile's cleanup removes.
        """
        self.db.conn.execute(
            "DELETE FROM pending_chunks WHERE local_path = ? "
            "AND (size_bytes != ? OR mtime_ns != ?)",
            (local_path, size_bytes, mtime_ns),
        )
        self.db.conn.commit()

    def completed(
        self, local_path: str, size_bytes: int, mtime_ns: int
    ) -> list[sqlite3.Row]:
        """Rows for chunks already uploaded for this exact file, in order."""
        return self.db.conn.execute(
            "SELECT sequence, account_id, drive_file_id, md5, size_bytes "
            "FROM pending_chunks WHERE local_path = ? AND size_bytes = ? "
            "AND mtime_ns = ? ORDER BY sequence",
            (local_path, size_bytes, mtime_ns),
        ).fetchall()

    def record(
        self,
        local_path: str,
        size_bytes: int,
        mtime_ns: int,
        sequence: int,
        account_id: int,
        drive_file_id: str,
        md5: str,
        chunk_size: int,
    ) -> None:
        self.db.conn.execute(
            "INSERT INTO pending_chunks(local_path, size_bytes, mtime_ns, sequence, "
            "account_id, drive_file_id, md5, chunk_size) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(local_path, sequence) DO UPDATE SET "
            "size_bytes = excluded.size_bytes, mtime_ns = excluded.mtime_ns, "
            "account_id = excluded.account_id, drive_file_id = excluded.drive_file_id, "
            "md5 = excluded.md5, chunk_size = excluded.chunk_size",
            (
                local_path,
                size_bytes,
                mtime_ns,
                sequence,
                account_id,
                drive_file_id,
                md5,
                chunk_size,
            ),
        )
        self.db.conn.commit()

    def clear(self, local_path: str) -> None:
        self.db.conn.execute(
            "DELETE FROM pending_chunks WHERE local_path = ?", (local_path,)
        )
        self.db.conn.commit()

    def drive_ids(self) -> set[str]:
        """Every pending chunk's Drive id (protected from orphan cleanup)."""
        rows = self.db.conn.execute(
            "SELECT drive_file_id FROM pending_chunks"
        ).fetchall()
        return {r["drive_file_id"] for r in rows}
