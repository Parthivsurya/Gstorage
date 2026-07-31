"""A single account's row in the dashboard: email, usage bar, and figures."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QProgressBar, QVBoxLayout, QWidget

from gstore.auth.accounts import Account
from gstore.format import human_bytes


class AccountRow(QWidget):
    def __init__(self, account: Account) -> None:
        super().__init__()

        self.email = QLabel(account.email)
        self.email.setStyleSheet("font-weight: 600;")

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)

        self.detail = QLabel()
        self.detail.setStyleSheet("color: #999;")

        layout = QVBoxLayout()
        layout.setContentsMargins(4, 6, 4, 6)
        layout.setSpacing(3)
        layout.addWidget(self.email)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail)
        self.setLayout(layout)

        self.set_account(account)

    def set_account(self, account: Account) -> None:
        self.email.setText(account.email)

        if account.is_revoked:
            self.email.setStyleSheet("font-weight: 600; color: #d06666;")
            self.bar.setRange(0, 100)
            self.bar.setValue(0)
            self.detail.setText(
                "⚠ needs re-link — click “Add Google account” and sign in again "
                "to restore access to its files"
            )
            return

        self.email.setStyleSheet("font-weight: 600;")

        if account.quota_synced_at is None:
            self.bar.setRange(0, 0)  # indeterminate: not fetched yet
            self.detail.setText("syncing…")
            return

        used = account.quota_used_bytes or 0
        limit = account.quota_limit_bytes

        if limit is None:  # unlimited
            self.bar.setRange(0, 0)
            self.detail.setText(f"{human_bytes(used)} used · unlimited")
            return

        free = max(0, limit - used)
        percent = int(used * 100 / limit) if limit > 0 else 0
        self.bar.setRange(0, 100)
        self.bar.setValue(percent)
        self.detail.setText(
            f"{human_bytes(used)} / {human_bytes(limit)} used"
            f"  ·  {human_bytes(free)} free"
        )
