"""Uploads a whole local folder, one file at a time, off the UI thread.

Reuses :meth:`UploadWorker._upload_one` rather than reimplementing placement:
each file independently gets the best-fit account, falls back to chunking
across several when no single account holds it, and resumes an interrupted
transfer. One copy of that logic, not two that drift.

The structure is recreated in the **index**, not on Drive. Drive stays flat —
everything lands in the account's Gstore folder (see
:mod:`gstore.drive.folder`) — while the folder tree the user sees in the app
is the virtual one the index has always kept. That is the same split the app
already had; a folder upload just populates it from disk.

Three behaviours worth knowing:

* **One bad file does not abandon the rest.** Failures are counted and
  reported at the end, the way the tidy-up handles a file Drive will not move.
* **Re-running skips what is already there.** A file whose name already exists
  in its target folder is left alone, so a folder upload interrupted at file
  300 of 500 carries on rather than duplicating the first 300.
* **Stopping is a pause.** The current file keeps its resumable session or its
  staged chunks, exactly as a single-file upload does.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.folder import FolderResolver
from gstore.drive.placement import free_bytes
from gstore.drive.quota import fetch_storage_quota
from gstore.format import human_bytes
from gstore.drive.rate_limit import Cancelled
from gstore.index import IndexStore
from gstore.index.pending_chunks import PendingChunks
from gstore.index.uploads import UploadSessions
from gstore.local_tree import scan_folder
from gstore.ui.upload_worker import UploadWorker


def build_folder_tree(
    index, root_name: str, parent_id: int | None, dirs: list[tuple[str, ...]]
) -> dict[tuple[str, ...], int | None]:
    """Recreate `dirs` under a new `root_name` folder; return rel -> folder id.

    `dirs` must arrive parents-first (:func:`gstore.local_tree.scan_folder`
    guarantees it) because each level is created under the id of the one
    above. `get_or_create_folder` makes this safe to run twice, which is what
    a re-run after an interruption does.
    """
    root_id = index.get_or_create_folder(root_name, parent_id)
    ids: dict[tuple[str, ...], int | None] = {(): root_id}
    for rel in dirs:
        ids[rel] = index.get_or_create_folder(rel[-1], ids[rel[:-1]])
    return ids


def already_there(index, parent_id: int | None, name: str) -> bool:
    """Whether this folder already holds a file of that name.

    Makes a re-run after an interruption carry on rather than upload the
    first three hundred files a second time.
    """
    return any(e.name == name for e in index.list_files(parent_id))


class FolderUploadWorker(UploadWorker):
    """Walks a folder and uploads every file in it into the index tree."""

    # done, total -- which file of how many, for the overall label.
    overall = Signal(int, int)
    # uploaded, skipped, failed
    finished_summary = Signal(int, int, int)
    # name, reason -- a file that could not be uploaded; the run continues.
    file_failed = Signal(str, str)

    def __init__(
        self, folder_path: str, parent_id: int | None, parent=None, plan=None
    ) -> None:
        # local_path is set per file as the walk proceeds; the base class reads
        # it for each upload.
        super().__init__(folder_path, parent_id, parent)
        self.folder_path = Path(folder_path)
        self.root_parent_id = parent_id
        # The UI scans first so it can show the counts before anything starts;
        # reusing that plan avoids walking the tree a second time.
        self.plan = plan

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        index = IndexStore(db)
        sessions = UploadSessions(db)
        pending = PendingChunks(db)
        folders = FolderResolver(accounts)
        uploaded = skipped = failed = 0
        try:
            account_list = accounts.list_accounts()
            if not account_list:
                self.failed.emit("Link a Google account first (Storage tab).")
                return

            self.status.emit("Looking through the folder…")
            plan = self.plan if self.plan is not None else scan_folder(
                self.folder_path
            )
            if not plan.files and not plan.dirs:
                self.failed.emit(
                    f"{self.folder_path.name} has nothing in it to upload."
                )
                return

            # Recreate the tree first, so even empty folders survive the trip
            # and every file has somewhere to land.
            folder_ids = build_folder_tree(
                index, self.folder_path.name, self.root_parent_id, plan.dirs
            )

            # Once for the batch, not once per file: 500 files would otherwise
            # mean 500 quota reads. Both upload paths write the quota back as
            # they finish, so placement stays honest between files.
            self.status.emit("Checking account space…")
            for account in account_list:
                try:
                    quota = fetch_storage_quota(
                        accounts.get_credentials(account.id)
                    )
                    accounts.update_quota(
                        account.id, quota.limit_bytes, quota.used_bytes
                    )
                except Exception:
                    pass  # unreachable account is skipped for placement

            # Say it now, not at file 300 of 500. Files are placed one at a
            # time across whichever accounts have room, so what matters is the
            # combined free space, not any single account's.
            fresh = accounts.list_accounts()
            unlimited = any(
                a.quota_limit_bytes is None and a.quota_synced_at is not None
                for a in fresh
            )
            combined_free = sum(free_bytes(a) or 0 for a in fresh)
            if not unlimited and plan.total_bytes > combined_free:
                self.failed.emit(
                    f"{self.folder_path.name} is {human_bytes(plan.total_bytes)}, "
                    f"but only {human_bytes(combined_free)} is free across your "
                    f"{len(fresh)} linked account(s). Link another account, or "
                    "upload part of the folder."
                )
                return

            total = len(plan.files)
            for number, planned in enumerate(plan.files, start=1):
                if self.isInterruptionRequested():
                    self.cancelled.emit()
                    return

                target_folder = folder_ids[planned.rel_dir]
                self.overall.emit(number, total)
                self.status.emit(f"{planned.path.name}  ({number} of {total})")

                if already_there(index, target_folder, planned.path.name):
                    skipped += 1
                    continue

                self.local_path = planned.path
                self.parent_id = target_folder
                try:
                    self._upload_one(
                        accounts, index, sessions, pending, folders,
                        refresh_quotas=False,
                    )
                    uploaded += 1
                except Cancelled:
                    self.cancelled.emit()
                    return
                except Exception as exc:  # noqa: BLE001 -- counted, not fatal
                    failed += 1
                    self.file_failed.emit(planned.path.name, str(exc))

            self.finished_summary.emit(uploaded, skipped, failed)
        except Cancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        finally:
            db.close()
