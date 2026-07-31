"""Reconciles the index against what actually exists in each Drive.

For every account: prune index files whose Drive file is gone (deleted outside
Gstore), and recover Drive files missing from the index into a "Recovered"
folder. Makes the index self-heal after external edits.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.reconcile import list_app_files
from gstore.index import IndexStore

_RECOVERED_FOLDER = "Recovered"


class ReconcileWorker(QThread):
    # accounts_checked, pruned, recovered
    finished_summary = Signal(int, int, int)
    account_failed = Signal(str, str)  # email, message

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        index = IndexStore(db)
        checked = pruned = recovered = 0
        try:
            for account in accounts.list_accounts():
                try:
                    credentials = accounts.get_credentials(account.id)
                    drive_files = list_app_files(credentials)
                except Exception as exc:
                    self.account_failed.emit(account.email, str(exc))
                    continue
                checked += 1

                # Prune: in the index but no longer on Drive.
                for entry in index.list_files_for_account(account.id):
                    if entry.drive_file_id and entry.drive_file_id not in drive_files:
                        index.delete_file(entry.id)
                        pruned += 1

                # Chunk part-files belong to chunked files, not the files table;
                # don't mistake them for orphans to recover.
                chunk_ids = index.chunk_drive_ids(account.id)

                # Recover: on Drive but missing from the index.
                recovered_folder_id: int | None = None
                for drive_id, meta in drive_files.items():
                    if drive_id in chunk_ids:
                        continue
                    if index.find_file_by_drive_id(account.id, drive_id) is not None:
                        continue
                    if recovered_folder_id is None:
                        recovered_folder_id = index.get_or_create_folder(
                            _RECOVERED_FOLDER, None
                        )
                    index.create_file(
                        meta.get("name", "untitled"),
                        recovered_folder_id,
                        int(meta.get("size") or 0),
                        account.id,
                        drive_id,
                        md5=meta.get("md5Checksum"),
                    )
                    recovered += 1

            self.finished_summary.emit(checked, pruned, recovered)
        finally:
            db.close()
