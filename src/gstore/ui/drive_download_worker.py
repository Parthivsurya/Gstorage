"""Downloads a file picked from the live Drive scan, off the UI thread.

Unlike :class:`gstore.ui.download_worker.DownloadWorker` (which resolves an
indexed Gstore file), this streams an arbitrary Drive file straight from a given
account by its Drive id — the file need not be in Gstore's index at all.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from google.auth.exceptions import RefreshError

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.download import download_file


class DriveDownloadWorker(QThread):
    progress = Signal(float)  # 0.0 - 1.0
    succeeded = Signal(str)  # destination path
    failed = Signal(str)

    def __init__(
        self,
        account_id: int,
        drive_file_id: str,
        dest_path: str,
        md5: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.account_id = account_id
        self.drive_file_id = drive_file_id
        self.dest_path = dest_path
        self.md5 = md5

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        try:
            try:
                credentials = accounts.get_credentials(self.account_id)
            except RefreshError:
                self.failed.emit(
                    "That account's access was revoked or expired. Re-link it in "
                    "the Storage tab, then retry."
                )
                return

            download_file(
                credentials,
                self.drive_file_id,
                self.dest_path,
                on_progress=self.progress.emit,
                expected_md5=self.md5,
            )
            self.succeeded.emit(self.dest_path)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            db.close()
