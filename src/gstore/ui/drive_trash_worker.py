"""Moves a file picked from the live Drive scan to Trash, off the UI thread.

Acts on an arbitrary Drive file by account + id (the file need not be in Gstore's
index). Uses a reversible trash rather than a permanent delete — the scan spans
the user's whole Drive, so a recoverable action is the safe default. Requires the
account to hold the full `drive` scope.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from google.auth.exceptions import RefreshError

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.browse import can_modify
from gstore.drive.delete import trash_drive_file


class DriveTrashWorker(QThread):
    succeeded = Signal()
    failed = Signal(str)

    def __init__(
        self, account_id: int, drive_file_id: str, parent=None
    ) -> None:
        super().__init__(parent)
        self.account_id = account_id
        self.drive_file_id = drive_file_id

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
            if not can_modify(credentials):
                # The UI only offers Trash for accounts the scan marked
                # writable, but guard here too: a token can lose the scope
                # between scan and click, and Google would answer 403.
                self.failed.emit(
                    "That account was linked without whole-Drive access, so "
                    "Gstore can only trash files it created itself. Enable "
                    "whole-Drive access and re-link the account to trash this."
                )
                return
            trash_drive_file(credentials, self.drive_file_id)
            self.succeeded.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            db.close()
