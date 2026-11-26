"""Downloads a whole indexed folder to disk, rebuilding its structure.

The mirror of :mod:`gstore.ui.folder_upload_worker`. It reuses
:meth:`DownloadWorker._download_one`, so each file independently takes the
whole-file or chunked path, reassembles across accounts, and is checksum
verified — one copy of that logic, not two.

A folder's files routinely live on **different accounts**, and a single file
inside it may be split across several more. None of that is special-cased
here: the index already records where each piece is, and the existing
download path already follows it.

* **One bad file does not abandon the rest**, the same as the folder upload.
* **A file already on disk at the right size is skipped**, so an interrupted
  folder download carries on rather than fetching the first 300 again.
* **Stopping is a pause** — a chunked file keeps its progress rows.
"""

from __future__ import annotations

from concurrent.futures import (
    ALL_COMPLETED,
    ThreadPoolExecutor,
    as_completed,
    wait,
)
from pathlib import Path

from PySide6.QtCore import Signal

from gstore.applog import get as get_logger
from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.chunk_upload import stream_count
from gstore.drive.download import download_file
from gstore.drive.rate_limit import Cancelled
from gstore.index import IndexStore
from gstore.ui.download_worker import DownloadWorker

log = get_logger(__name__)


def relative_dirs(index, root_folder_id: int, parent_id: int | None) -> tuple[str, ...]:
    """Folder names between `root_folder_id` and `parent_id`, exclusive.

    `()` means the file sits directly in the chosen folder. Everything above
    the chosen folder is dropped — downloading "2024" must not recreate the
    whole chain of its ancestors inside the destination the user picked.
    """
    chain = index.folder_path(parent_id)  # virtual root -> parent, inclusive
    names: list[str] = []
    seen_root = False
    for folder in chain:
        if seen_root:
            names.append(folder.name)
        elif folder.id == root_folder_id:
            seen_root = True
    return tuple(names)


def safe_name(name: str) -> str:
    """Make one path component safe to write.

    An index name is whatever the user typed, and a '/' in it would silently
    write outside the folder they chose. '..' is flattened for the same
    reason.
    """
    cleaned = name.replace("/", "_").replace("\\", "_").strip()
    if cleaned in {"", ".", ".."}:
        return "_"
    return cleaned


class FolderDownloadWorker(DownloadWorker):
    """Walks an indexed folder and downloads every file under it."""

    overall = Signal(int, int)            # done, total
    finished_summary = Signal(int, int, int)  # downloaded, skipped, failed
    file_failed = Signal(str, str)        # name, reason

    def __init__(
        self, folder_id: int, dest_root: str, folder_name: str, parent=None
    ) -> None:
        # file_id / dest_path are set per file as the walk proceeds; the base
        # class reads them for each download.
        super().__init__(folder_id, dest_root, parent)
        self.folder_id = folder_id
        self.dest_root = Path(dest_root)
        self.folder_name = folder_name
        # Across the whole folder, not per file: see the upload worker.
        self._batch_bytes = 0
        self._bytes_done = 0
        self._current_size = 0
        self._done = 0
        self._stopped = False

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        index = IndexStore(db)
        downloaded = skipped = failed = 0
        try:
            entries = index.descendant_files(self.folder_id)
            if not entries:
                self.failed.emit(f"{self.folder_name} has no files in it.")
                return

            root = self.dest_root / safe_name(self.folder_name)
            total = len(entries)
            self._batch_bytes = sum(e.size_bytes for e in entries)
            self._bytes_done = 0
            self.total_bytes.emit(self._batch_bytes)
            self.progress.emit(0.0)

            # Decide every destination up front, on this thread: resolving
            # one reads the index, and a worker may not reach back into a
            # SQLite connection it does not own.
            jobs = []
            for entry in entries:
                rel = relative_dirs(index, self.folder_id, entry.parent_id)
                target_dir = root.joinpath(*(safe_name(p) for p in rel))
                jobs.append((entry, target_dir / safe_name(entry.name)))

            pending = []
            for entry, destination in jobs:
                if self._already_downloaded(destination, entry.size_bytes):
                    skipped += 1
                    self._bytes_done += entry.size_bytes
                    continue
                if not entry.is_available:
                    failed += 1
                    self.file_failed.emit(
                        entry.name, "its account is not linked right now"
                    )
                    self._bytes_done += entry.size_bytes
                    continue
                pending.append((entry, destination))
            self._report()

            # Whole files fetch in parallel. Chunked ones stay sequential:
            # each already pulls its own pieces across accounts at the same
            # width, and stacking the two multiplies the streams against a
            # shared per-user rate limit.
            simple = [j for j in pending if not j[0].is_chunked]
            chunked = [j for j in pending if j[0].is_chunked]
            total = len(pending)
            self._done = 0

            got, fell = self._download_in_parallel(
                simple, accounts, total
            )
            downloaded += got
            failed += fell
            if self._stopped:
                self.cancelled.emit()
                return

            for entry, destination in chunked:
                if self.isInterruptionRequested():
                    self.cancelled.emit()
                    return
                self._done += 1
                self._current_size = entry.size_bytes
                self.overall.emit(self._done, total)
                self.status.emit(f"{entry.name}  ({self._done} of {total})")
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    self.file_id = entry.id
                    self.dest_path = str(destination)
                    error = self._download_one(accounts, index, db)
                    if error:
                        failed += 1
                        self.file_failed.emit(entry.name, error)
                    else:
                        downloaded += 1
                except Cancelled:
                    self.cancelled.emit()
                    return
                except Exception as exc:  # noqa: BLE001 -- counted, not fatal
                    failed += 1
                    log.exception("folder download: %s failed", entry.name)
                    self.file_failed.emit(entry.name, f"{type(exc).__name__}: {exc}")
                self._bytes_done += entry.size_bytes
                self._current_size = 0
                self._report()

            self.finished_summary.emit(downloaded, skipped, failed)
        except Cancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            db.close()

    def _download_in_parallel(self, jobs, accounts, total) -> tuple[int, int]:
        """Fetch the whole-file downloads concurrently.

        Only the network and the write to disk happen in the pool. Index
        reads were all done before it started, and nothing here writes to the
        database — which is what makes this simpler than the upload side.
        """
        downloaded = failed = 0
        self._stopped = False
        if not jobs:
            return 0, 0

        creds = {
            entry.home_account_id: accounts.get_credentials(entry.home_account_id)
            for entry, _ in jobs
        }
        for _, destination in jobs:
            destination.parent.mkdir(parents=True, exist_ok=True)

        def fetch(position: int):
            entry, destination = jobs[position]
            download_file(
                creds[entry.home_account_id],
                entry.drive_file_id,
                destination,
                expected_md5=entry.md5,
            )
            return entry

        workers = min(stream_count() * 2, len(jobs))
        log.info(
            "folder download: %d whole file(s) over %d streams", len(jobs), workers
        )
        handled: set = set()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch, i): i for i in range(len(jobs))}
            for future in as_completed(futures):
                if self.isInterruptionRequested():
                    self._stopped = True
                    break
                handled.add(future)
                got, fell = self._settle(future, futures, jobs, total)
                downloaded += got
                failed += fell

            if self._stopped:
                for waiting in futures:
                    waiting.cancel()
                finished, _ = wait(futures, return_when=ALL_COMPLETED)
                for future in finished:
                    if future in handled or future.cancelled():
                        continue
                    got, fell = self._settle(
                        future, futures, jobs, total, announce=False
                    )
                    downloaded += got
                    failed += fell
        return downloaded, failed

    def _settle(
        self, future, futures, jobs, total, announce: bool = True
    ) -> tuple[int, int]:
        """Account for one finished download. Returns (downloaded, failed)."""
        entry, _destination = jobs[futures[future]]
        self._bytes_done += entry.size_bytes
        try:
            future.result()
        except Cancelled:
            self._stopped = True
            return 0, 0
        except Exception as exc:  # noqa: BLE001 -- counted, not fatal
            self.file_failed.emit(entry.name, f"{type(exc).__name__}: {exc}")
            if announce:
                self._done += 1
                self.overall.emit(self._done, total)
            self._report()
            return 0, 1
        if announce:
            self._done += 1
            self.overall.emit(self._done, total)
            self.status.emit(f"{entry.name}  ({self._done} of {total})")
        self._report()
        return 1, 0

    def _emit_total(self, size_bytes: int) -> None:
        """Ignored: the total shown is the folder's, set once at the start."""

    def _report(self) -> None:
        if self._batch_bytes:
            self.progress.emit(min(self._bytes_done / self._batch_bytes, 1.0))

    def _emit_progress(self, fraction: float) -> None:
        """One file's own progress, placed inside the folder's total."""
        if not self._batch_bytes:
            return
        done = self._bytes_done + fraction * self._current_size
        self.progress.emit(min(done / self._batch_bytes, 1.0))

    @staticmethod
    def _already_downloaded(destination: Path, size_bytes: int) -> bool:
        """Whether this file is already on disk at its full size.

        Size, not checksum: re-hashing every file would read the whole folder
        twice before fetching a byte. A truncated file has the wrong size and
        is fetched again; a complete one was already checksum-verified when it
        landed.
        """
        try:
            return destination.is_file() and destination.stat().st_size == size_bytes
        except OSError:
            return False
