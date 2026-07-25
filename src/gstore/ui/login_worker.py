"""Runs the blocking OAuth login off the UI thread.

The consent flow opens a browser and waits for the loopback redirect, which can
take many seconds — doing it on the GUI thread would freeze the window.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from gstore.auth.oauth import fetch_account_email, run_login_flow
from gstore.config import client_secrets_path


class LoginWorker(QThread):
    # (credentials, email)
    succeeded = Signal(object, str)
    failed = Signal(str)

    def run(self) -> None:  # noqa: D102 (QThread entry point)
        try:
            credentials = run_login_flow(client_secrets_path())
            email = fetch_account_email(credentials)
            self.succeeded.emit(credentials, email)
        except Exception as exc:  # surfaced to the user via the failed signal
            self.failed.emit(str(exc))
