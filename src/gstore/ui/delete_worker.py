"""Deletes a file or folder from both Drive and the index, off the UI thread.

A folder's files may live on several accounts, so each real Drive file is
deleted individually. If any Drive deletion fails (e.g. a revoked account), that
file's index row is kept and the folder itself is preserved so the user can
retry after fixing the account — we never orphan a still-existing Drive file.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from google.auth.exceptions import RefreshError

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.delete import delete_drive_file
from gstore.index import IndexStore


class DeleteWorker(QThread):
    # number of Drive deletions that failed (0 == fully clean)
    succeeded = Signal(int)
    failed = Signal(str)

    def __init__(self, kind: str, item_id: int, parent=None) -> None:
        super().__init__(parent)
        self.kind = kind  # "file" or "folder"
        self.item_id = item_id

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        index = IndexStore(db)
        try:
            if self.kind == "file":
                self._delete_file(accounts, index)
            else:
                self._delete_folder(accounts, index)
        except RefreshError as exc:
            self.failed.emit(
                "An account's access was revoked or expired. Re-link it in the "
                "Storage tab, then retry."
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            db.close()

    def _delete_file(self, accounts: AccountStore, index: IndexStore) -> None:
        entry = index.get_file(self.item_id)
        if entry is None:
            self.succeeded.emit(0)
            return
        if entry.drive_file_id and entry.home_account_id is not None:
            credentials = accounts.get_credentials(entry.home_account_id)
            delete_drive_file(credentials, entry.drive_file_id)
        index.delete_file(self.item_id)
        self.succeeded.emit(0)

    def _delete_folder(self, accounts: AccountStore, index: IndexStore) -> None:
        failures = 0
        for entry in index.descendant_files(self.item_id):
            if entry.drive_file_id and entry.home_account_id is not None:
                try:
                    credentials = accounts.get_credentials(entry.home_account_id)
                    delete_drive_file(credentials, entry.drive_file_id)
                    index.delete_file(entry.id)
                except Exception:
                    failures += 1  # keep this row; folder stays for retry
            else:
                index.delete_file(entry.id)

        if failures == 0:
            index.delete_folder(self.item_id)
        self.succeeded.emit(failures)
