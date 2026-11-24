"""Runs a file upload off the UI thread.

Placement is whole-file first: refresh quotas, pick the best-fit account, and
resumable-upload the file. If no single account has room, the file is split into
chunks spread across several accounts (Phase B). Uses its own SQLite connection
(see QuotaWorker for rationale).
"""

from __future__ import annotations

from pathlib import Path

from google.auth.exceptions import RefreshError
from PySide6.QtCore import QThread, Signal

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.chunk_upload import ChunkUploadResult, upload_chunks
from gstore.drive.hashing import md5_of_file
from gstore.drive.placement import (
    CHUNK_SIZE,
    choose_account,
    chunk_ranges,
    plan_missing_chunks,
)
from gstore.drive.quota import fetch_storage_quota
from gstore.drive.rate_limit import Cancelled
from gstore.drive.folder import FolderResolver
from gstore.drive.upload import upload_file
from gstore.index import IndexStore
from gstore.index.pending_chunks import PendingChunks
from gstore.index.uploads import DbUploadSession, UploadSessions

_REVOKED_HINT = "Re-link it in the Storage tab, then retry."


class _ChunkResume:
    """Adapts PendingChunks to the resume interface upload_chunks expects.

    Bridges the DB-only pending store and the drive layer's ChunkUploadResult,
    so neither has to know about the other.
    """

    def __init__(self, pending: PendingChunks, local_path: str, size: int, mtime: int):
        self._pending = pending
        self._path = local_path
        self._size = size
        self._mtime = mtime
        # Persisted alongside each chunk so a later resume can skip re-hashing.
        self.file_md5: str | None = None
        pending.reset_if_changed(local_path, size, mtime)

    def cached_file_md5(self) -> str | None:
        return self._pending.file_md5(self._path, self._size, self._mtime)

    def completed(self) -> dict[int, ChunkUploadResult]:
        return {
            row["sequence"]: ChunkUploadResult(
                sequence=row["sequence"],
                account_id=row["account_id"],
                drive_file_id=row["drive_file_id"],
                md5=row["md5"],
                # chunk_size, NOT size_bytes: the latter is the whole file's
                # size, stored as part of this row's identity key.
                size_bytes=row["chunk_size"],
            )
            for row in self._pending.completed(self._path, self._size, self._mtime)
        }

    def record(self, result: ChunkUploadResult) -> None:
        self._pending.record(
            self._path,
            self._size,
            self._mtime,
            result.sequence,
            result.account_id,
            result.drive_file_id,
            result.md5,
            result.size_bytes,
            self.file_md5,
        )

    def clear(self) -> None:
        self._pending.clear(self._path)


class UploadWorker(QThread):
    progress = Signal(float)  # 0.0 - 1.0
    succeeded = Signal(str, str)  # file name, placement description
    failed = Signal(str)
    # Distinct from `failed`: nothing went wrong and the partial transfer is
    # kept, so re-picking the same file continues from where it stopped.
    cancelled = Signal()
    # Which phase the worker is in, so the UI label is not stuck on "Uploading…"
    # while the file is actually being checksummed.
    status = Signal(str)

    def __init__(self, local_path: str, parent_id: int | None, parent=None) -> None:
        super().__init__(parent)
        self.local_path = Path(local_path)
        self.parent_id = parent_id

    def _emit_progress(self, fraction: float) -> None:
        """One file's 0.0-1.0 completion.

        A hook rather than a direct emit so FolderUploadWorker can scale it
        into the whole batch: during a folder upload a single file reaching
        100% is not the transfer reaching 100%.
        """
        self.progress.emit(fraction)

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        index = IndexStore(db)
        sessions = UploadSessions(db)
        pending = PendingChunks(db)
        # Resolved on this thread, which owns the SQLite connection, and cached
        # for the whole transfer: see gstore.drive.folder.FolderResolver.
        folders = FolderResolver(accounts)
        try:
            if not accounts.list_accounts():
                self.failed.emit("Link a Google account first (Storage tab).")
                return
            self._upload_one(accounts, index, sessions, pending, folders)
        except Cancelled:
            # Paused, not failed: the resumable session / staged chunks remain.
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            db.close()

    def _upload_one(
        self,
        accounts,
        index,
        sessions,
        pending,
        folders,
        refresh_quotas: bool = True,
    ) -> None:
        """Place and upload `self.local_path` into `self.parent_id`.

        Split out of :meth:`run` so FolderUploadWorker can drive it once per
        file instead of reimplementing placement, resume and chunking — one
        copy of that logic, not two that drift apart.

        `refresh_quotas` is False when the caller has already refreshed them
        for this batch. A folder of 500 files would otherwise re-read every
        account's quota 500 times; both upload paths write the quota back when
        they finish, so the recorded figures stay accurate between files.
        """
        account_list = accounts.list_accounts()

        stat = self.local_path.stat()
        size, mtime_ns = stat.st_size, stat.st_mtime_ns
        path_key = str(self.local_path)

        # Resume an interrupted whole-file upload on its original account if
        # one exists and the file is unchanged; otherwise place it fresh.
        target = None
        resume = sessions.find(path_key, size, mtime_ns)
        if resume is not None:
            target = next((a for a in account_list if a.id == resume[0]), None)
            if target is None:
                sessions.clear(path_key)  # home account is gone

        if target is None:
            if refresh_quotas:
                # Refresh quotas so placement reflects real free space.
                for account in account_list:
                    try:
                        creds = accounts.get_credentials(account.id)
                        quota = fetch_storage_quota(creds)
                        accounts.update_quota(
                            account.id, quota.limit_bytes, quota.used_bytes
                        )
                    except Exception:
                        pass  # unreachable account is skipped for placement

            fresh = accounts.list_accounts()
            target = choose_account(fresh, size)
            if target is None:
                # No single account fits — split across several.
                self._upload_chunked(
                    accounts, index, pending, fresh, size, mtime_ns, path_key,
                    folders,
                )
                return

        self._upload_whole(
            accounts, index, sessions, target, size, mtime_ns, path_key, folders
        )

    # -- whole-file -----------------------------------------------------------

    def _upload_whole(
        self, accounts, index, sessions, target, size, mtime_ns, path_key, folders
    ) -> None:
        try:
            credentials = accounts.get_credentials(target.id)
        except RefreshError:
            self.failed.emit(
                f"Access to {target.email} was revoked or expired. {_REVOKED_HINT}"
            )
            return

        session = DbUploadSession(sessions, target.id, path_key, size, mtime_ns)
        result = upload_file(
            credentials,
            self.local_path,
            on_progress=self._emit_progress,
            session=session,
            should_stop=self.isInterruptionRequested,
            parent_id=folders.folder_id(target.id),
        )
        index.create_file(
            self.local_path.name,
            self.parent_id,
            result.size_bytes,
            target.id,
            result.drive_file_id,
            md5=result.md5,
        )

        try:
            quota = fetch_storage_quota(credentials)
            accounts.update_quota(target.id, quota.limit_bytes, quota.used_bytes)
        except Exception:
            pass

        self.succeeded.emit(self.local_path.name, target.email)

    # -- chunked --------------------------------------------------------------

    def _upload_chunked(
        self, accounts, index, pending, candidates, size, mtime_ns, path_key, folders
    ) -> None:
        resume = _ChunkResume(pending, path_key, size, mtime_ns)
        completed = resume.completed()

        # Staged chunks are only reusable if they were cut to the same layout
        # we are about to plan. If CHUNK_SIZE changed between attempts — or the
        # rows predate chunk_size being recorded — mixing the two would stitch
        # together overlapping or gapped byte ranges, and the result would only
        # be caught by the whole-file checksum at download time. Start over
        # instead; the abandoned parts are orphans that reconcile cleans up.
        expected = {seq: length for seq, _offset, length in chunk_ranges(size, CHUNK_SIZE)}
        if any(expected.get(seq) != r.size_bytes for seq, r in completed.items()):
            resume.clear()
            resume = _ChunkResume(pending, path_key, size, mtime_ns)
            completed = resume.completed()

        # Plan only the chunks not already uploaded (resume-aware).
        assignments = plan_missing_chunks(
            candidates, size, set(completed), CHUNK_SIZE
        )
        if assignments is None:
            self.failed.emit(
                "No combination of linked accounts has enough free space "
                "for this file."
            )
            return

        # Hashing 19 GB takes minutes. Doing it on every retry, with the bar
        # pinned at zero, is what made a resumed upload look hung — so the
        # first attempt's digest is cached against the file's identity and
        # every later attempt reuses it.
        whole_md5 = resume.cached_file_md5()
        if whole_md5 is None:
            self.status.emit("Checksumming…")
            whole_md5 = md5_of_file(
                self.local_path,
                on_progress=self._emit_progress,
                should_stop=self.isInterruptionRequested,
            )
        resume.file_md5 = whole_md5
        self.status.emit("Uploading…")
        # The bar was driven by the checksum pass; hand it back to the upload
        # at the fraction already staged. Emitting 0.0 here would be a lie the
        # UI cannot see through: it anchors the speed estimate, so a resume at
        # 43% would be credited with having just sent those 8.7 GB.
        already = sum(r.size_bytes for r in completed.values())
        self.progress.emit(already / size if size else 0.0)

        creds_cache: dict[int, object] = {}

        def credentials_for(account_id: int):
            creds = creds_cache.get(account_id)
            if creds is None:
                creds = accounts.get_credentials(account_id)  # may RefreshError
                creds_cache[account_id] = creds
            return creds

        try:
            results = upload_chunks(
                self.local_path,
                assignments,
                credentials_for,
                on_progress=self._emit_progress,
                resume=resume,
                should_stop=self.isInterruptionRequested,
                parent_for=folders.folder_id,
            )
        except RefreshError:
            # Uploaded chunks are staged; the next attempt resumes from them.
            self.failed.emit(
                "An account this file is being split across was revoked or "
                f"expired. Uploaded chunks were kept to resume. {_REVOKED_HINT}"
            )
            return

        file_id = index.create_file(
            self.local_path.name, self.parent_id, size, None, None, md5=whole_md5
        )
        for result in results:
            index.add_chunk(
                file_id,
                result.sequence,
                result.account_id,
                result.drive_file_id,
                result.md5,
                result.size_bytes,
            )
        resume.clear()  # staged chunks now live in `chunks`

        involved = {r.account_id for r in results}
        for account_id in involved:
            try:
                quota = fetch_storage_quota(credentials_for(account_id))
                accounts.update_quota(
                    account_id, quota.limit_bytes, quota.used_bytes
                )
            except Exception:
                pass

        self.succeeded.emit(
            self.local_path.name,
            f"{len(results)} chunks across {len(involved)} account(s)",
        )
