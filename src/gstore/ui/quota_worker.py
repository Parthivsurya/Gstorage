"""Fetches storage quotas for all accounts off the UI thread.

Opens its own SQLite connection because sqlite3 objects are bound to the thread
that created them; WAL mode (enabled at open) lets this coexist with the main
thread's reads. Each account is fetched independently so one revoked or offline
account doesn't abort the rest.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.quota import fetch_storage_quota


class QuotaWorker(QThread):
    # account_id whose snapshot was just written to the DB
    account_updated = Signal(int)
    # account_id, error message
    account_failed = Signal(int, str)

    def run(self) -> None:  # noqa: D102 (QThread entry point)
        db = Database().open()
        store = AccountStore(db)
        try:
            for account in store.list_accounts():
                try:
                    credentials = store.get_credentials(account.id)
                    quota = fetch_storage_quota(credentials)
                    store.update_quota(account.id, quota.limit_bytes, quota.used_bytes)
                    self.account_updated.emit(account.id)
                except Exception as exc:
                    self.account_failed.emit(account.id, str(exc))
        finally:
            db.close()
