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
        """Rows for chunks already uploaded for this exact file, in order.

        The `size_bytes` argument identifies the *file* — it is half the
        identity key, with mtime — and is deliberately not returned. The
        length of each chunk is `chunk_size`; reading the file size as if it
        were the chunk length is exactly the bug this column exists to prevent.
        """
        return self.db.conn.execute(
            "SELECT sequence, account_id, drive_file_id, md5, chunk_size "
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
        file_md5: str | None = None,
    ) -> None:
        self.db.conn.execute(
            "INSERT INTO pending_chunks(local_path, size_bytes, mtime_ns, sequence, "
            "account_id, drive_file_id, md5, chunk_size, file_md5) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(local_path, sequence) DO UPDATE SET "
            "size_bytes = excluded.size_bytes, mtime_ns = excluded.mtime_ns, "
            "account_id = excluded.account_id, drive_file_id = excluded.drive_file_id, "
            "md5 = excluded.md5, chunk_size = excluded.chunk_size, "
            "file_md5 = excluded.file_md5",
            (
                local_path,
                size_bytes,
                mtime_ns,
                sequence,
                account_id,
                drive_file_id,
                md5,
                chunk_size,
                file_md5,
            ),
        )
        self.db.conn.commit()

    def file_md5(self, local_path: str, size_bytes: int, mtime_ns: int) -> str | None:
        """The cached whole-file digest for this exact file, if one was stored.

        Lets a resumed upload skip re-reading the entire file purely to
        recompute a hash the first attempt already paid for.
        """
        row = self.db.conn.execute(
            "SELECT file_md5 FROM pending_chunks WHERE local_path = ? "
            "AND size_bytes = ? AND mtime_ns = ? AND file_md5 IS NOT NULL LIMIT 1",
            (local_path, size_bytes, mtime_ns),
        ).fetchone()
        return row["file_md5"] if row else None

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
