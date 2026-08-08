"""Fetches Drive thumbnail images for the Gallery view, off the UI thread.

Drive's `thumbnailLink` is a short-lived, auth-required URL, so each image is
pulled with the owning account's OAuth credentials via an AuthorizedSession.
Thumbnails are emitted one at a time as they arrive (keyed by Drive file id) so
the grid can fill in progressively; per-image failures are skipped silently —
a missing thumbnail just leaves the generic file icon in place.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal
from google.auth.transport.requests import AuthorizedSession

from gstore.auth.accounts import AccountStore
from gstore.db import Database

# (account_id, drive_file_id, thumbnail_url)
ThumbnailJob = tuple[int, str, str]


class ThumbnailWorker(QThread):
    ready = Signal(str, bytes)  # drive_file_id, image bytes

    def __init__(self, jobs: list[ThumbnailJob], parent=None) -> None:
        super().__init__(parent)
        self._jobs = jobs

    def run(self) -> None:
        db = Database().open()
        accounts = AccountStore(db)
        sessions: dict[int, AuthorizedSession] = {}
        try:
            for account_id, drive_file_id, url in self._jobs:
                if self.isInterruptionRequested():
                    return
                try:
                    session = sessions.get(account_id)
                    if session is None:
                        session = AuthorizedSession(accounts.get_credentials(account_id))
                        sessions[account_id] = session
                    response = session.get(url, timeout=(5, 10))
                    if response.status_code == 200 and response.content:
                        self.ready.emit(drive_file_id, response.content)
                except Exception:
                    continue  # skip this one; keep the generic icon
        finally:
            db.close()
