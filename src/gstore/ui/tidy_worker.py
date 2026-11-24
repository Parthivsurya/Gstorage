"""Moves files uploaded before the Gstore folder existed into it.

One pass per linked account. An account that cannot be reached is reported and
skipped rather than stopping the run — the other accounts are still worth
tidying.

Nothing here touches the index: a Drive move keeps the file id, which is the
only thing the index stores, so the map from a virtual file to its bytes is
unaffected and every live share link keeps working.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.folder import FolderResolver
from gstore.drive.tidy import Cancelled, tidy_account


class TidyWorker(QThread):
    # accounts_tidied, files_moved, files_that_would_not_move
    finished_summary = Signal(int, int, int)
    account_failed = Signal(str, str)  # email, message
    progress = Signal(str, int, int)  # email, done, total
    cancelled = Signal()

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        # Resolved on this thread, which owns the connection.
        folders = FolderResolver(accounts)
        tidied = moved = failed = 0
        try:
            for account in accounts.list_accounts():
                if self.isInterruptionRequested():
                    self.cancelled.emit()
                    return
                if account.is_revoked:
                    self.account_failed.emit(
                        account.email, "access was revoked; re-link it first"
                    )
                    continue

                folder_id = folders.folder_id(account.id)
                if folder_id is None:
                    self.account_failed.emit(
                        account.email, "could not open or create its Gstore folder"
                    )
                    continue

                try:
                    result = tidy_account(
                        accounts.get_credentials(account.id),
                        folder_id,
                        should_stop=self.isInterruptionRequested,
                        on_progress=lambda done, total, _e=account.email: (
                            self.progress.emit(_e, done, total)
                        ),
                    )
                except Cancelled:
                    self.cancelled.emit()
                    return
                except Exception as exc:  # noqa: BLE001 — reported, not fatal
                    self.account_failed.emit(account.email, str(exc))
                    continue

                tidied += 1
                moved += result.moved
                failed += result.failed

            self.finished_summary.emit(tidied, moved, failed)
        finally:
            db.close()
