"""Runs a whole-file upload off the UI thread.

Flow: refresh account quotas (so placement is accurate) -> pick the best-fit
account -> resumable-upload the file -> record it in the index -> refresh that
account's usage. Uses its own SQLite connection (see QuotaWorker for rationale).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.placement import choose_account
from gstore.drive.quota import fetch_storage_quota
from gstore.drive.upload import upload_file
from gstore.index import IndexStore


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
        try:
            if not accounts.list_accounts():
                self.failed.emit("Link a Google account first (Storage tab).")
                return

            size = self.local_path.stat().st_size

            # Refresh quotas so placement reflects real free space.
            for account in accounts.list_accounts():
                try:
                    creds = accounts.get_credentials(account.id)
                    quota = fetch_storage_quota(creds)
                    accounts.update_quota(account.id, quota.limit_bytes, quota.used_bytes)
                except Exception:
                    pass  # unreachable account is simply skipped for placement

            target = choose_account(accounts.list_accounts(), size)
            if target is None:
                self.failed.emit(
                    "No linked account has enough free space for this file."
                )
                return

            credentials = accounts.get_credentials(target.id)
            drive_id, real_size = upload_file(
                credentials, self.local_path, on_progress=self.progress.emit
            )
            index.create_file(
                self.local_path.name, self.parent_id, real_size, target.id, drive_id
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
