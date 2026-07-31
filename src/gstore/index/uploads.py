"""Persistence for in-flight resumable upload sessions.

An interrupted upload (dropped connection, crash, quit) can resume instead of
restarting if we remember Drive's resumable session URI. A session is tied to a
local file's identity — path, size, and mtime — so editing or replacing the file
invalidates the stored session and the upload starts fresh.

:class:`DbUploadSession` adapts this store to the tiny load/save/clear interface
:func:`gstore.drive.upload.upload_file` expects, keeping the drive layer free of
any database dependency.
"""

from __future__ import annotations


class UploadSessions:
    """CRUD over the `uploads` table."""

    def __init__(self, db) -> None:
        self.db = db

    def find(
        self, local_path: str, size_bytes: int, mtime_ns: int
    ) -> tuple[int, str] | None:
        """Return (account_id, resumable_uri) for a still-valid session, else None.

        A session matches only if the file's size and mtime are unchanged since
        it was recorded; otherwise the local file differs and resuming would
        append to the wrong bytes.
        """
        row = self.db.conn.execute(
            "SELECT account_id, resumable_uri FROM uploads "
            "WHERE local_path = ? AND size_bytes = ? AND mtime_ns = ?",
            (local_path, size_bytes, mtime_ns),
        ).fetchone()
        if row is None:
            return None
        return int(row["account_id"]), row["resumable_uri"]

    def save(
        self,
        account_id: int,
        local_path: str,
        size_bytes: int,
        mtime_ns: int,
        resumable_uri: str,
    ) -> None:
        """Record (or replace) the session URI for a local path."""
        self.db.conn.execute(
            "INSERT INTO uploads(account_id, local_path, size_bytes, mtime_ns, "
            "resumable_uri) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(local_path) DO UPDATE SET "
            "account_id = excluded.account_id, size_bytes = excluded.size_bytes, "
            "mtime_ns = excluded.mtime_ns, resumable_uri = excluded.resumable_uri",
            (account_id, local_path, size_bytes, mtime_ns, resumable_uri),
        )
        self.db.conn.commit()

    def clear(self, local_path: str) -> None:
        self.db.conn.execute("DELETE FROM uploads WHERE local_path = ?", (local_path,))
        self.db.conn.commit()


class DbUploadSession:
    """load/save/clear for one (account, local file) pair, backed by the store."""

    def __init__(
        self,
        sessions: UploadSessions,
        account_id: int,
        local_path: str,
        size_bytes: int,
        mtime_ns: int,
    ) -> None:
        self._sessions = sessions
        self._account_id = account_id
        self._local_path = local_path
        self._size_bytes = size_bytes
        self._mtime_ns = mtime_ns

    def load(self) -> str | None:
        found = self._sessions.find(self._local_path, self._size_bytes, self._mtime_ns)
        if found is None or found[0] != self._account_id:
            return None
        return found[1]

    def save(self, resumable_uri: str) -> None:
        self._sessions.save(
            self._account_id,
            self._local_path,
            self._size_bytes,
            self._mtime_ns,
            resumable_uri,
        )

    def clear(self) -> None:
        self._sessions.clear(self._local_path)
