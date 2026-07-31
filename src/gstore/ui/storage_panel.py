"""Storage tab: combined-usage dashboard plus account management.

Owns the OAuth login and quota-refresh workers, since both concern the linked
accounts shown here.
"""

from __future__ import annotations

from google.oauth2.credentials import Credentials
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gstore.auth.accounts import AccountStore
from gstore.db import Database
from gstore.drive.quota import aggregate
from gstore.format import human_bytes
from gstore.index import IndexStore
from gstore.ui.account_row import AccountRow
from gstore.ui.login_worker import LoginWorker
from gstore.ui.quota_worker import QuotaWorker
from gstore.ui.reconcile_worker import ReconcileWorker


class StoragePanel(QWidget):
    # Emitted when the set of linked accounts changes (added/removed).
    accounts_changed = Signal()
    # Emitted when a reconcile changes the file index (prune/recover).
    index_changed = Signal()

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db
        self.accounts = AccountStore(db)
        self.index = IndexStore(db)
        self._login_worker: LoginWorker | None = None
        self._quota_worker: QuotaWorker | None = None
        self._reconcile_worker: ReconcileWorker | None = None
        self._quota_errors: list[str] = []

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
        self.setLayout(layout)

        self._refresh_dashboard()
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

        self.repair_button = QPushButton("Repair / re-sync")
        self.repair_button.setToolTip(
            "Reconcile the index with each Drive: drop files deleted elsewhere, "
            "recover app files missing from the index."
        )
        self.repair_button.clicked.connect(self._start_reconcile)

        self.button_row = QHBoxLayout()
        self.button_row.addWidget(self.add_button)
        self.button_row.addWidget(self.remove_button)
        self.button_row.addWidget(self.refresh_button)
        self.button_row.addWidget(self.repair_button)
        self.button_row.addStretch()

    # -- rendering ------------------------------------------------------------

    def reload(self) -> None:
        """Re-render from the index after an external change (e.g. an upload)."""
        self._refresh_dashboard()

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
            self.summary_bar.setRange(0, 0)
            self.summary_detail.setText("Fetching usage…")
            return

        if combined.total_limit is None:
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
        self._refresh_dashboard()

    def _on_quota_failed(self, account_id: int, message: str) -> None:
        self._quota_errors.append(f"account {account_id}: {message}")
        # get_credentials may have just flagged this account revoked; re-render
        # so its row (and any dependent files) show the unavailable state.
        self._refresh_dashboard()
        self.index_changed.emit()

    def _on_quota_finished(self) -> None:
        self._quota_worker = None
        self.refresh_button.setEnabled(True)
        if self._quota_errors:
            self.status.setText(
                f"Usage updated, {len(self._quota_errors)} account(s) failed"
            )
        else:
            self.status.setText("Usage up to date")

    # -- repair / reconcile ---------------------------------------------------

    def _start_reconcile(self) -> None:
        if self._reconcile_worker is not None:
            return
        if self.accounts.count() == 0:
            self.status.setText("Link an account before repairing.")
            return
        self.repair_button.setEnabled(False)
        self.status.setText("Repairing / re-syncing…")

        worker = ReconcileWorker(self)
        worker.finished_summary.connect(self._on_reconcile_summary)
        worker.account_failed.connect(self._on_reconcile_account_failed)
        worker.finished.connect(self._on_reconcile_finished)
        self._reconcile_worker = worker
        worker.start()

    def _on_reconcile_summary(
        self, checked: int, pruned: int, recovered: int
    ) -> None:
        self.status.setText(
            f"Re-synced {checked} account(s): "
            f"{pruned} pruned, {recovered} recovered."
        )
        if pruned or recovered:
            self.index_changed.emit()

    def _on_reconcile_account_failed(self, email: str, message: str) -> None:
        self._quota_errors.append(f"{email}: {message}")

    def _on_reconcile_finished(self) -> None:
        self._reconcile_worker = None
        self.repair_button.setEnabled(True)

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
        self.accounts_changed.emit()
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
        account_id = int(item.data(Qt.ItemDataRole.UserRole))
        row = self.account_list.itemWidget(item)
        email = row.email.text() if isinstance(row, AccountRow) else "this account"

        hosted = len(self.index.list_files_for_account(account_id))
        message = (
            f"Remove {email} from Gstore?\n\n"
            "This only unlinks it locally; nothing in Google Drive is deleted."
        )
        if hosted:
            # Removal orphans these files: without the account's token their bytes
            # can't be reached. Re-linking instead keeps them accessible.
            message += (
                f"\n\n⚠ {hosted} file(s) live on this account and will become "
                "unavailable in Gstore. To keep access, re-link the account "
                "instead of removing it."
            )
        confirm = QMessageBox.question(self, "Remove account", message)
        if confirm == QMessageBox.StandardButton.Yes:
            self.accounts.remove_account(account_id)
            self._refresh_dashboard()
            self.accounts_changed.emit()
            if hosted:
                self.index_changed.emit()  # files just became unavailable
