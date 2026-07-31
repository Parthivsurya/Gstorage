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
from gstore.drive.chunk_upload import upload_chunks
from gstore.drive.hashing import md5_of_file
from gstore.drive.placement import choose_account, plan_chunk_placement
from gstore.drive.quota import fetch_storage_quota
from gstore.drive.upload import upload_file
from gstore.index import IndexStore
from gstore.index.uploads import DbUploadSession, UploadSessions

_REVOKED_HINT = "Re-link it in the Storage tab, then retry."


class UploadWorker(QThread):
    progress = Signal(float)  # 0.0 - 1.0
    succeeded = Signal(str, str)  # file name, placement description
    failed = Signal(str)

    def __init__(self, local_path: str, parent_id: int | None, parent=None) -> None:
        super().__init__(parent)
        self.local_path = Path(local_path)
        self.parent_id = parent_id

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        index = IndexStore(db)
        sessions = UploadSessions(db)
        try:
            account_list = accounts.list_accounts()
            if not account_list:
                self.failed.emit("Link a Google account first (Storage tab).")
                return

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
                    self._upload_chunked(accounts, index, fresh, size)
                    return

            self._upload_whole(
                accounts, index, sessions, target, size, mtime_ns, path_key
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            db.close()

    # -- whole-file -----------------------------------------------------------

    def _upload_whole(
        self, accounts, index, sessions, target, size, mtime_ns, path_key
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
            on_progress=self.progress.emit,
            session=session,
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

    def _upload_chunked(self, accounts, index, candidates, size) -> None:
        assignments = plan_chunk_placement(candidates, size)
        if assignments is None:
            self.failed.emit(
                "No combination of linked accounts has enough free space "
                "for this file."
            )
            return

        whole_md5 = md5_of_file(self.local_path)
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
                on_progress=self.progress.emit,
            )
        except RefreshError:
            self.failed.emit(
                "An account this file was being split across was revoked or "
                f"expired; the partial upload was cleaned up. {_REVOKED_HINT}"
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
