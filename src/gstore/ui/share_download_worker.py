"""Opening someone else's share, off the UI thread.

Split in two because the flow is: read the manifest, show the user what the
link actually points at, let them pick a destination, then download. Asking
for a destination before knowing the file's name or size would be asking
them to decide blind.

Neither worker touches the index or any credentials. This is the one path in
the app that runs with no account at all, which is the whole point of it.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from gstore.applog import get as get_logger
from gstore.share.fetch import ShareUnavailable, download_shared, load_manifest
from gstore.share.manifest import InvalidLink, InvalidManifest, decode_link

log = get_logger(__name__)


class ResolveShareWorker(QThread):
    """Fetch and decrypt the manifest a link points at."""

    # The Manifest itself; it is not a Qt type, so it rides as an object.
    resolved = Signal(object)
    failed = Signal(str)

    def __init__(self, link: str, parent=None) -> None:
        super().__init__(parent)
        self.link = link

    def run(self) -> None:
        try:
            manifest_id, key = decode_link(self.link)
        except (InvalidLink, ValueError) as exc:
            self.failed.emit(str(exc))
            return

        try:
            self.resolved.emit(load_manifest(manifest_id, key))
        except (ShareUnavailable, InvalidManifest) as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            log.exception("resolving a share link failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class ShareDownloadWorker(QThread):
    """Rebuild a shared file at a chosen destination."""

    # qint64: a shared file is as large as any other, and Signal(int) is a
    # 32-bit C++ int — a 20 GB total wraps to a negative number.
    progress = Signal("qint64", "qint64")
    succeeded = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, manifest, dest_path: str, parent=None) -> None:
        super().__init__(parent)
        self.manifest = manifest
        self.dest_path = dest_path
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            download_shared(
                self.manifest,
                self.dest_path,
                should_stop=lambda: self._stop,
                on_progress=lambda done, total: self.progress.emit(done, total),
            )
        except ShareUnavailable as exc:
            if self._stop:
                self.cancelled.emit()
            else:
                self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("downloading a shared file failed")
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.succeeded.emit(self.dest_path)
