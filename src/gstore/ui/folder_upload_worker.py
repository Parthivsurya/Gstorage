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

from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path

from PySide6.QtCore import Signal

from gstore.applog import get as get_logger
from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.chunk_upload import stream_count
from gstore.drive.folder import FolderResolver
from gstore.drive.placement import free_bytes, plan_file_placement
from gstore.drive.upload import SIMPLE_MAX_BYTES, upload_file
from gstore.drive.quota import fetch_storage_quota
from gstore.format import human_bytes
from gstore.drive.rate_limit import Cancelled
from gstore.index import IndexStore
from gstore.index.pending_chunks import PendingChunks
from gstore.index.uploads import UploadSessions
from gstore.local_tree import scan_folder
from gstore.ui.upload_worker import UploadWorker

log = get_logger(__name__)


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
        self._done = 0
        self._stopped = False
        # Progress is reported across the whole batch, not per file: one file
        # of 282 reaching 100% is not the upload reaching 100%.
        self._batch_bytes = 0
        self._bytes_done = 0
        self._current_size = 0

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

            # Anything already in its target folder is settled before a single
            # request goes out, so a resumed run does no network work for it.
            todo = []
            for planned in plan.files:
                if already_there(index, folder_ids[planned.rel_dir],
                                 planned.path.name):
                    skipped += 1
                else:
                    todo.append(planned)

            # Small files go up in parallel; the big ones run one at a time
            # because each already spreads its own chunks across accounts at
            # the same width, and stacking the two would multiply the streams.
            small = [p for p in todo if p.size_bytes <= SIMPLE_MAX_BYTES]
            large = [p for p in todo if p.size_bytes > SIMPLE_MAX_BYTES]
            total = len(todo)
            self._done = 0
            # Bytes, not file count: these files run from 20 KB to 2 MB, so
            # "45 of 282" and "16% of the bytes" are different numbers and
            # only the second predicts how long is left.
            self._batch_bytes = sum(p.size_bytes for p in todo)
            self._bytes_done = 0
            self.total_bytes.emit(self._batch_bytes)
            self.progress.emit(0.0)

            up, fail = self._upload_in_parallel(
                small, folder_ids, accounts, index, folders, fresh, total
            )
            uploaded += up
            failed += fail
            if self._stopped:
                self.cancelled.emit()
                return

            for planned in large:
                if self.isInterruptionRequested():
                    self.cancelled.emit()
                    return
                self._done += 1
                self.overall.emit(self._done, total)
                self.status.emit(
                    f"{planned.path.name}  ({self._done} of {total})"
                )
                self.local_path = planned.path
                self.parent_id = folder_ids[planned.rel_dir]
                self._current_size = planned.size_bytes
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
                self._bytes_done += planned.size_bytes
                self._current_size = 0
                self._report()

            self.finished_summary.emit(uploaded, skipped, failed)
        except Cancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
        finally:
            db.close()

    def _report(self) -> None:
        """Push the batch's completion, counting bytes rather than files."""
        if self._batch_bytes:
            self.progress.emit(min(self._bytes_done / self._batch_bytes, 1.0))

    def _emit_progress(self, fraction: float) -> None:
        """A large file's own progress, placed inside the batch's total."""
        if not self._batch_bytes:
            return
        done = self._bytes_done + fraction * self._current_size
        self.progress.emit(min(done / self._batch_bytes, 1.0))

    def _upload_in_parallel(
        self, planned_files, folder_ids, accounts, index, folders,
        account_list, total,
    ) -> tuple[int, int]:
        """Send the small files concurrently; record each as it lands.

        Only the network happens in the pool. Every index write stays on this
        thread, because a SQLite connection belongs to the thread that opened
        it — the same rule the chunked upload follows for its credentials.

        Accounts are assigned for the whole batch up front rather than per
        file: workers running at the same time cannot each ask which account
        has the most room, or they would all be told the same one and fill it
        past its quota.
        """
        uploaded = failed = 0
        self._stopped = False
        if not planned_files:
            return 0, 0

        assignment = plan_file_placement(
            account_list, [p.size_bytes for p in planned_files]
        )
        creds = {
            a.id: accounts.get_credentials(a.id)
            for a in account_list
            if a.id in {x for x in assignment if x is not None}
        }
        parents = {aid: folders.folder_id(aid) for aid in creds}

        def send(index_in_batch: int):
            planned = planned_files[index_in_batch]
            account_id = assignment[index_in_batch]
            if account_id is None:
                raise RuntimeError("no linked account has room for this file")
            return planned, account_id, upload_file(
                creds[account_id],
                planned.path,
                parent_id=parents.get(account_id),
            )

        workers = min(stream_count() * 2, max(1, len(planned_files)))
        log.info(
            "folder upload: %d small file(s) over %d streams",
            len(planned_files), workers,
        )
        recorded: set = set()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(send, i): i for i in range(len(planned_files))
            }
            for future in as_completed(futures):
                if self.isInterruptionRequested():
                    self._stopped = True
                    break
                recorded.add(future)
                up, fl = self._record(
                    future, futures, planned_files, folder_ids, index, total
                )
                uploaded += up
                failed += fl

            if self._stopped:
                # Stop what has not started, then wait for what is already in
                # the air and record it anyway. A file that reached Drive
                # without an index row is an orphan: the next run would upload
                # a second copy, and only Repair / re-sync would ever find the
                # first. Cancelling must not manufacture those.
                for pending in futures:
                    pending.cancel()
                finished, _ = wait(futures, return_when=ALL_COMPLETED)
                landed = 0
                for future in finished:
                    if future in recorded or future.cancelled():
                        continue
                    up, fl = self._record(
                        future, futures, planned_files, folder_ids, index,
                        total, announce=False,
                    )
                    uploaded += up
                    failed += fl
                    landed += up
                if landed:
                    log.info("recorded %d file(s) that landed while stopping", landed)
        return uploaded, failed

    def _record(
        self, future, futures, planned_files, folder_ids, index, total,
        announce: bool = True,
    ) -> tuple[int, int]:
        """Write one finished upload into the index. Returns (uploaded, failed).

        Called from the worker thread in both the normal loop and the drain
        after a cancel, so the two cannot disagree about what counts as
        recorded.
        """
        planned = planned_files[futures[future]]
        try:
            planned, account_id, result = future.result()
        except Cancelled:
            self._stopped = True
            return 0, 0
        except Exception as exc:  # noqa: BLE001 -- counted, not fatal
            self._bytes_done += planned.size_bytes
            self.file_failed.emit(
                planned.path.name, f"{type(exc).__name__}: {exc}"
            )
            if announce:
                self._done += 1
                self.overall.emit(self._done, total)
            self._report()
            return 0, 1

        index.create_file(
            planned.path.name,
            folder_ids[planned.rel_dir],
            result.size_bytes,
            account_id,
            result.drive_file_id,
            md5=result.md5,
        )
        self._bytes_done += planned.size_bytes
        if announce:
            self._done += 1
            self.overall.emit(self._done, total)
            self.status.emit(f"{planned.path.name}  ({self._done} of {total})")
        self._report()
        return 1, 0
