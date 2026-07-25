"""Main application window — combined storage dashboard.

Phase 2: shows the pooled storage total across all linked accounts plus a
per-account breakdown. Quotas are fetched from Drive about.get on a worker
thread and cached in the local index, so the dashboard renders instantly from
cache on launch and then refreshes live.
"""

from __future__ import annotations

from google.oauth2.credentials import Credentials
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gstore import __version__
from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.quota import aggregate
from gstore.format import human_bytes
from gstore.ui.account_row import AccountRow
from gstore.ui.login_worker import LoginWorker
from gstore.ui.quota_worker import QuotaWorker


class MainWindow(QMainWindow):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.accounts = AccountStore(db)
        self._login_worker: LoginWorker | None = None
        self._quota_worker: QuotaWorker | None = None
        self._quota_errors: list[str] = []

        self.setWindowTitle(f"Gstore {__version__}")
        self.resize(760, 560)

        self._build_summary()
        self._build_account_list()
        self._build_buttons()

        self.status = QLabel("")
        self.status.setStyleSheet("color: #888;")

        layout = QVBoxLayout()
        layout.addWidget(self.summary_frame)
        layout.addWidget(self.accounts_title)
        layout.addWidget(self.account_list)
        layout.addLayout(self.button_row)
        layout.addWidget(self.status)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self._refresh_dashboard()
        # Kick off a live quota refresh if any accounts exist.
        if self.accounts.count() > 0:
            self._start_quota_refresh()

    # -- widget construction --------------------------------------------------

    def _build_summary(self) -> None:
        self.summary_frame = QFrame()
        self.summary_frame.setFrameShape(QFrame.Shape.StyledPanel)

        heading = QLabel("Combined storage")
        heading.setStyleSheet("font-size: 13px; color: #888;")

        self.summary_headline = QLabel("—")
        self.summary_headline.setStyleSheet("font-size: 26px; font-weight: 700;")

        self.summary_bar = QProgressBar()
        self.summary_bar.setTextVisible(False)
        self.summary_bar.setFixedHeight(10)

        self.summary_detail = QLabel("No accounts linked yet.")
        self.summary_detail.setStyleSheet("color: #999;")

        layout = QVBoxLayout()
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addWidget(heading)
        layout.addWidget(self.summary_headline)
        layout.addWidget(self.summary_bar)
        layout.addWidget(self.summary_detail)
        self.summary_frame.setLayout(layout)

    def _build_account_list(self) -> None:
        self.accounts_title = QLabel("Accounts")
        self.accounts_title.setStyleSheet(
            "font-size: 15px; font-weight: 600; margin-top: 8px;"
        )
        self.account_list = QListWidget()
        self.account_list.currentItemChanged.connect(self._update_buttons)

    def _build_buttons(self) -> None:
        self.add_button = QPushButton("Add Google account")
        self.add_button.clicked.connect(self._on_add_clicked)

        self.remove_button = QPushButton("Remove selected")
        self.remove_button.clicked.connect(self._on_remove_clicked)
        self.remove_button.setEnabled(False)

        self.refresh_button = QPushButton("Refresh usage")
        self.refresh_button.clicked.connect(self._start_quota_refresh)

        self.button_row = QHBoxLayout()
        self.button_row.addWidget(self.add_button)
        self.button_row.addWidget(self.remove_button)
        self.button_row.addWidget(self.refresh_button)
        self.button_row.addStretch()

    # -- rendering ------------------------------------------------------------

    def _refresh_dashboard(self) -> None:
        accounts = self.accounts.list_accounts()

        self.account_list.clear()
        for account in accounts:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, account.id)
            row = AccountRow(account)
            item.setSizeHint(row.sizeHint())
            self.account_list.addItem(item)
            self.account_list.setItemWidget(item, row)

        self._render_summary(accounts)
        self._update_buttons()

    def _render_summary(self, accounts: list) -> None:
        if not accounts:
            self.summary_headline.setText("—")
            self.summary_bar.setRange(0, 100)
            self.summary_bar.setValue(0)
            self.summary_detail.setText("No accounts linked yet.")
            return

        combined = aggregate(accounts)

        if combined.synced_count == 0:
            self.summary_headline.setText("—")
            self.summary_bar.setRange(0, 0)  # indeterminate
            self.summary_detail.setText("Fetching usage…")
            return

        if combined.total_limit is None:  # an unlimited account is in the pool
            self.summary_headline.setText(
                f"{human_bytes(combined.total_used)} used · unlimited"
            )
            self.summary_bar.setRange(0, 0)
            self.summary_detail.setText(
                f"{combined.synced_count} of {len(accounts)} account(s) synced"
            )
            return

        free = combined.total_free or 0
        percent = (
            int(combined.total_used * 100 / combined.total_limit)
            if combined.total_limit > 0
            else 0
        )
        self.summary_headline.setText(f"{human_bytes(free)} free")
        self.summary_bar.setRange(0, 100)
        self.summary_bar.setValue(percent)
        self.summary_detail.setText(
            f"{human_bytes(combined.total_used)} used of "
            f"{human_bytes(combined.total_limit)}  ·  "
            f"{combined.synced_count} of {len(accounts)} account(s) synced"
        )

    def _update_buttons(self) -> None:
        self.remove_button.setEnabled(self.account_list.currentItem() is not None)

    # -- quota refresh --------------------------------------------------------

    def _start_quota_refresh(self) -> None:
        if self._quota_worker is not None:
            return
        if self.accounts.count() == 0:
            return
        self._quota_errors = []
        self.refresh_button.setEnabled(False)
        self.status.setText("Refreshing usage…")

        worker = QuotaWorker(self)
        worker.account_updated.connect(self._on_quota_updated)
        worker.account_failed.connect(self._on_quota_failed)
        worker.finished.connect(self._on_quota_finished)
        self._quota_worker = worker
        worker.start()

    def _on_quota_updated(self, account_id: int) -> None:
        # The worker wrote the snapshot on its own connection; re-read and render.
        self._refresh_dashboard()

    def _on_quota_failed(self, account_id: int, message: str) -> None:
        self._quota_errors.append(f"account {account_id}: {message}")

    def _on_quota_finished(self) -> None:
        self._quota_worker = None
        self.refresh_button.setEnabled(True)
        if self._quota_errors:
            self.status.setText(
                f"Usage updated, {len(self._quota_errors)} account(s) failed"
            )
        else:
            self.status.setText("Usage up to date")

    # -- add account ----------------------------------------------------------

    def _on_add_clicked(self) -> None:
        if self._login_worker is not None:
            return
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
        self._refresh_dashboard()
        self._start_quota_refresh()

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
        row = self.account_list.itemWidget(item)
        email = row.email.text() if isinstance(row, AccountRow) else "this account"
        confirm = QMessageBox.question(
            self,
            "Remove account",
            f"Remove {email} from Gstore?\n\n"
            "This only unlinks it locally; nothing in Google Drive is deleted.",
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.accounts.remove_account(int(account_id))
            self._refresh_dashboard()
