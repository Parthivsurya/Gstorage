"""Asks GitHub for a newer release, off the UI thread.

Deliberately quiet: it reports only when there is something to say. No
"you're up to date" dialog, no error popup when the machine is offline — a
failed check is not the user's problem, and the next launch tries again.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from gstore import config
from gstore.applog import get as get_logger
from gstore.update import Release, check_for_update

log = get_logger(__name__)


class UpdateWorker(QThread):
    # Only ever emitted for a release newer than the one running, that the
    # user has not already chosen to skip.
    update_available = Signal(object)  # Release

    def __init__(self, current_version: str, parent=None) -> None:
        super().__init__(parent)
        self.current_version = current_version

    def run(self) -> None:
        if not config.update_check_enabled():
            log.info("update check is switched off")
            return
        try:
            release: Release | None = check_for_update(self.current_version)
        except Exception as exc:  # pragma: no cover - belt and braces
            log.info("update check failed: %s: %s", type(exc).__name__, exc)
            return
        if release is None:
            return
        if release.version in config.skipped_versions():
            log.info("update %s was skipped by the user", release.version)
            return
        self.update_available.emit(release)
