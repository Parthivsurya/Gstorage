"""Downloads an indexed file to disk off the UI thread."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from google.auth.exceptions import RefreshError

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.chunk_download import download_chunks
from gstore.drive.download import download_file
from gstore.index import IndexStore


class DownloadWorker(QThread):
    progress = Signal(float)  # 0.0 - 1.0
    succeeded = Signal(str)  # destination path
    failed = Signal(str)

    def __init__(self, file_id: int, dest_path: str, parent=None) -> None:
        super().__init__(parent)
        self.file_id = file_id
        self.dest_path = dest_path

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        index = IndexStore(db)
        try:
            entry = index.get_file(self.file_id)
            if entry is None:
                self.failed.emit("This file is no longer in the index.")
                return

            if entry.is_chunked:
                self._download_chunked(accounts, index)
                return

            if not entry.drive_file_id or entry.home_account_id is None:
                self.failed.emit("This file has no stored Drive location yet.")
                return

            try:
                credentials = accounts.get_credentials(entry.home_account_id)
            except RefreshError:
                self.failed.emit(
                    f"Access to {entry.home_account_email} was revoked or "
                    "expired. Re-link it in the Storage tab, then retry."
                )
                return

            download_file(
                credentials,
                entry.drive_file_id,
                self.dest_path,
                on_progress=self.progress.emit,
                expected_md5=entry.md5,
            )
            self.succeeded.emit(self.dest_path)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            db.close()

    def _download_chunked(self, accounts, index) -> None:
        chunks = index.list_chunks(self.file_id)
        entry = index.get_file(self.file_id)
        creds_cache: dict[int, object] = {}

        def credentials_for(account_id: int):
            creds = creds_cache.get(account_id)
            if creds is None:
                creds = accounts.get_credentials(account_id)  # may RefreshError
                creds_cache[account_id] = creds
            return creds

        try:
            download_chunks(
                chunks,
                credentials_for,
                self.dest_path,
                expected_md5=entry.md5 if entry else None,
                on_progress=self.progress.emit,
            )
        except RefreshError:
            self.failed.emit(
                "An account holding part of this file was revoked or expired. "
                "Re-link it in the Storage tab, then retry."
            )
            return
        self.succeeded.emit(self.dest_path)
