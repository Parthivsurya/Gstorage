"""Runs a whole-file upload off the UI thread.

Flow: refresh account quotas (so placement is accurate) -> pick the best-fit
account -> resumable-upload the file -> record it in the index -> refresh that
account's usage. Uses its own SQLite connection (see QuotaWorker for rationale).
"""

from __future__ import annotations

from pathlib import Path

from google.auth.exceptions import RefreshError
from PySide6.QtCore import QThread, Signal

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.placement import choose_account
from gstore.drive.quota import fetch_storage_quota
from gstore.drive.upload import upload_file
from gstore.index import IndexStore
from gstore.index.uploads import DbUploadSession, UploadSessions


class UploadWorker(QThread):
    progress = Signal(float)  # 0.0 - 1.0
    succeeded = Signal(str, str)  # file name, account email
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

            # Resume an interrupted upload on its original account if one exists
            # and the file is unchanged; otherwise place the file fresh.
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

                target = choose_account(accounts.list_accounts(), size)
                if target is None:
                    self.failed.emit(
                        "No linked account has enough free space for this file."
                    )
                    return

            try:
                credentials = accounts.get_credentials(target.id)
            except RefreshError:
                self.failed.emit(
                    f"Access to {target.email} was revoked or expired. "
                    "Re-link it in the Storage tab, then retry."
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

            # Reflect the new usage on the chosen account.
            try:
                quota = fetch_storage_quota(credentials)
                accounts.update_quota(target.id, quota.limit_bytes, quota.used_bytes)
            except Exception:
                pass

            self.succeeded.emit(self.local_path.name, target.email)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            db.close()
