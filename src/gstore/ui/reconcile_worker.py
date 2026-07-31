"""Reconciles the index against what actually exists in each Drive.

For every account: prune index files whose Drive file is gone (deleted outside
Gstore), recover Drive files missing from the index into a "Recovered" folder,
and delete orphaned chunk part-files left by crashed/abandoned chunked uploads.
Makes the index self-heal after external edits.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.chunk_upload import PART_MARKER
from gstore.drive.delete import delete_drive_file
from gstore.drive.reconcile import list_app_files
from gstore.index import IndexStore
from gstore.index.pending_chunks import PendingChunks

_RECOVERED_FOLDER = "Recovered"


class ReconcileWorker(QThread):
    # accounts_checked, pruned, recovered, orphan_chunks_cleaned
    finished_summary = Signal(int, int, int, int)
    account_failed = Signal(str, str)  # email, message

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        index = IndexStore(db)
        pending = PendingChunks(db)
        checked = pruned = recovered = orphans = 0
        try:
            # Pending chunk part-files (in-flight resumable uploads) are protected
            # from orphan cleanup on every account.
            pending_ids = pending.drive_ids()

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
                # committed and pending chunks are both legitimate, not orphans.
                chunk_ids = index.chunk_drive_ids(account.id)
                protected = chunk_ids | pending_ids

                # Recover: on Drive but missing from the index.
                recovered_folder_id: int | None = None
                for drive_id, meta in drive_files.items():
                    if drive_id in protected:
                        continue

                    # A chunk part-file not referenced by any chunk (committed or
                    # pending) is a leftover from a crashed upload — delete it.
                    if PART_MARKER in meta.get("name", ""):
                        try:
                            delete_drive_file(credentials, drive_id)
                            orphans += 1
                        except Exception:
                            pass  # best-effort; retry on a later reconcile
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

            self.finished_summary.emit(checked, pruned, recovered, orphans)
        finally:
            db.close()
