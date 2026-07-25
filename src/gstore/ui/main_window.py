"""Main application window.

Phase 1: link multiple Google accounts via OAuth and list them. Each "Add
account" runs the PKCE loopback flow on a worker thread and persists encrypted
credentials. The combined storage view (Phase 2) and unified browser (Phase 3)
will replace the accounts panel with richer widgets.
"""

from __future__ import annotations

from google.oauth2.credentials import Credentials
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gstore import __version__
from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.ui.login_worker import LoginWorker


class MainWindow(QMainWindow):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.accounts = AccountStore(db)
        self._login_worker: LoginWorker | None = None

        self.setWindowTitle(f"Gstore {__version__}")
        self.resize(720, 480)

        title = QLabel("Linked Google accounts")
        title.setStyleSheet("font-size: 20px; font-weight: 600;")

        self.account_list = QListWidget()
        self.account_list.currentItemChanged.connect(self._update_buttons)

        self.add_button = QPushButton("Add Google account")
        self.add_button.clicked.connect(self._on_add_clicked)

        self.remove_button = QPushButton("Remove selected")
        self.remove_button.clicked.connect(self._on_remove_clicked)
        self.remove_button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.add_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch()

        self.status = QLabel("")
        self.status.setStyleSheet("color: #888;")

        layout = QVBoxLayout()
        layout.addWidget(title)
        layout.addWidget(self.account_list)
        layout.addLayout(buttons)
        layout.addWidget(self.status)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self._refresh_accounts()

    # -- account list ---------------------------------------------------------

    def _refresh_accounts(self) -> None:
        self.account_list.clear()
        accounts = self.accounts.list_accounts()
        for account in accounts:
            item = QListWidgetItem(account.email)
            item.setData(Qt.ItemDataRole.UserRole, account.id)
            self.account_list.addItem(item)
        self.status.setText(f"{len(accounts)} account(s) linked")
        self._update_buttons()

    def _update_buttons(self) -> None:
        self.remove_button.setEnabled(self.account_list.currentItem() is not None)

    # -- add account ----------------------------------------------------------

    def _on_add_clicked(self) -> None:
        if self._login_worker is not None:
            return  # a login is already in progress
        self.add_button.setEnabled(False)
        self.status.setText("Opening browser for Google sign-in…")

        worker = LoginWorker(self)
        worker.succeeded.connect(self._on_login_succeeded)
        worker.failed.connect(self._on_login_failed)
        worker.finished.connect(self._on_login_finished)
        self._login_worker = worker
        worker.start()

    def _on_login_succeeded(self, credentials: Credentials, email: str) -> None:
        self.accounts.add_account(credentials, email)
        self.status.setText(f"Linked {email}")
        self._refresh_accounts()

    def _on_login_failed(self, message: str) -> None:
        QMessageBox.warning(self, "Sign-in failed", message)
        self.status.setText("Sign-in failed")

    def _on_login_finished(self) -> None:
        self._login_worker = None
        self.add_button.setEnabled(True)

    # -- remove account -------------------------------------------------------

    def _on_remove_clicked(self) -> None:
        item = self.account_list.currentItem()
        if item is None:
            return
        account_id = item.data(Qt.ItemDataRole.UserRole)
        email = item.text()
        confirm = QMessageBox.question(
            self,
            "Remove account",
            f"Remove {email} from Gstore?\n\n"
            "This only unlinks it locally; nothing in Google Drive is deleted.",
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.accounts.remove_account(int(account_id))
            self._refresh_accounts()
