"""Fetches and verifies an update off the UI thread.

A 70-100 MB download would freeze the window for its duration on the UI
thread. Nothing here installs anything: the worker hands back a verified
file for the user to open.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from gstore.applog import get as get_logger
from gstore.updater.download import (
    DownloadCancelled,
    DownloadError,
    SignatureError,
    download_update,
)

log = get_logger(__name__)


class UpdateDownloadWorker(QThread):
    # read, total. qint64 because Signal(int) is a 32-bit C++ int and a
    # release asset is already a third of the way to overflowing it.
    progress = Signal("qint64", "qint64")
    succeeded = Signal(str)           # path to the verified file
    failed = Signal(str)
    # Separate from `failed`: a signature that does not check out is not a
    # network problem, and the user should be told plainly that what arrived
    # was not a genuine release rather than invited to retry.
    rejected = Signal(str)
    # Distinct from `failed`: the user pressed Cancel, and being shown a
    # warning for doing what they asked for is its own small insult.
    cancelled = Signal()

    def __init__(self, release, destination: Path, parent=None) -> None:
        super().__init__(parent)
        self.release = release
        self.destination = Path(destination)

    def run(self) -> None:
        try:
            result = download_update(
                list(self.release.assets),
                self.release.version,
                self.destination,
                on_progress=self.progress.emit,
                should_stop=self.isInterruptionRequested,
            )
        except DownloadCancelled:
            self.cancelled.emit()
            return
        except SignatureError as exc:
            log.error("update signature rejected: %s", exc)
            self.rejected.emit(str(exc))
            return
        except DownloadError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("update download failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.succeeded.emit(str(result.path))
