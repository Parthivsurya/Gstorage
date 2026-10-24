"""Downloads an indexed file to disk off the UI thread."""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from google.auth.exceptions import RefreshError

from gstore.applog import get as get_logger
from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.chunk_download import NotEnoughSpace, download_chunks
from gstore.drive.download import download_file
from gstore.drive.rate_limit import Cancelled
from gstore.index import IndexStore

log = get_logger(__name__)


class DownloadWorker(QThread):
    progress = Signal(float)  # 0.0 - 1.0
    succeeded = Signal(str)  # destination path
    failed = Signal(str)
    # Phase text, e.g. "Downloading chunk 212 of 409" — a bare percentage says
    # nothing about where a 409-chunk reassembly actually is.
    status = Signal(str)
    # Paused rather than failed: the part file is kept and resumes.
    cancelled = Signal()
    # Total bytes, once known, so the UI can show a rate and an ETA.
    total_bytes = Signal(int)

    def __init__(self, file_id: int, dest_path: str, parent=None) -> None:
        super().__init__(parent)
        self.file_id = file_id
        self.dest_path = dest_path

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        index = IndexStore(db)
        try:
            entry = index.get_file(self.file_id)
            if entry is None:
                self.failed.emit("This file is no longer in the index.")
                return

            if entry.is_chunked:
                self._download_chunked(accounts, index)
                return

            if not entry.drive_file_id or entry.home_account_id is None:
                self.failed.emit("This file has no stored Drive location yet.")
                return

            try:
                credentials = accounts.get_credentials(entry.home_account_id)
            except RefreshError:
                self.failed.emit(
                    f"Access to {entry.home_account_email} was revoked or "
                    "expired. Re-link it in the Storage tab, then retry."
                )
                return

            if entry.size_bytes:
                self.total_bytes.emit(entry.size_bytes)
            download_file(
                credentials,
                entry.drive_file_id,
                self.dest_path,
                on_progress=self.progress.emit,
                expected_md5=entry.md5,
            )
            self.succeeded.emit(self.dest_path)
        except Cancelled:
            self.cancelled.emit()
        except NotEnoughSpace as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            log.exception("download of file %s failed", self.file_id)
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            db.close()

    def _download_chunked(self, accounts, index) -> None:
        chunks = index.list_chunks(self.file_id)
        entry = index.get_file(self.file_id)
        creds_cache: dict[int, object] = {}

        def credentials_for(account_id: int):
            creds = creds_cache.get(account_id)
            if creds is None:
                creds = accounts.get_credentials(account_id)  # may RefreshError
                creds_cache[account_id] = creds
            return creds

        total = sum(c.size_bytes for c in chunks)
        if total:
            self.total_bytes.emit(total)

        def announce(number: int, count: int) -> None:
            self.status.emit(f"Downloading chunk {number} of {count}…")

        try:
            download_chunks(
                chunks,
                credentials_for,
                self.dest_path,
                expected_md5=entry.md5 if entry else None,
                on_progress=self.progress.emit,
                on_chunk=announce,
                should_stop=self.isInterruptionRequested,
            )
        except RefreshError:
            self.failed.emit(
                "An account holding part of this file was revoked or expired. "
                "Re-link it in the Storage tab, then retry."
            )
            return
        self.succeeded.emit(self.dest_path)
