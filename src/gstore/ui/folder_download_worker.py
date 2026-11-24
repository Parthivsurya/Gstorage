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

from pathlib import Path

from PySide6.QtCore import Signal

from gstore.applog import get as get_logger
from gstore.auth.accounts import AccountStore
from gstore.db import Database
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

            for number, entry in enumerate(entries, start=1):
                if self.isInterruptionRequested():
                    self.cancelled.emit()
                    return

                rel = relative_dirs(index, self.folder_id, entry.parent_id)
                target_dir = root.joinpath(*(safe_name(p) for p in rel))
                destination = target_dir / safe_name(entry.name)

                self.overall.emit(number, total)
                self.status.emit(f"{entry.name}  ({number} of {total})")

                if self._already_downloaded(destination, entry.size_bytes):
                    skipped += 1
                    continue

                if not entry.is_available:
                    failed += 1
                    self.file_failed.emit(
                        entry.name, "its account is not linked right now"
                    )
                    continue

                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
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

            self.finished_summary.emit(downloaded, skipped, failed)
        except Cancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            db.close()

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
