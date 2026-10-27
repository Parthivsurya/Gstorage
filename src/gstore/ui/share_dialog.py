"""The three dialogs sharing needs: confirm, show the link, open one.

The confirm dialog carries more weight than a usual "are you sure". Every
other destructive action in this app is reversible or local; this one puts
file content within reach of anyone the link reaches, and no later
withdrawal can recall a copy already taken. So it says that plainly, before
anything is granted, rather than afterwards in a tooltip.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from gstore.share.manifest import looks_like_link
from gstore.format import human_bytes

_MUTED = "color: #888;"


def _wrapped(text: str, *, muted: bool = False) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    if muted:
        label.setStyleSheet(_MUTED)
    return label


class ShareConfirmDialog(QDialog):
    """Explain what a share actually does, then ask."""

    def __init__(self, name: str, size_bytes: int, part_count: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Share this file?")
        self.setMinimumWidth(460)

        pieces = (
            f"Its {part_count} pieces will be made readable"
            if part_count > 1
            else "It will be made readable"
        )

        layout = QVBoxLayout()
        layout.addWidget(_wrapped(f"<b>{name}</b>"))
        layout.addWidget(_wrapped(human_bytes(size_bytes), muted=True))
        layout.addSpacing(10)
        layout.addWidget(
            _wrapped(
                f"{pieces} by anyone who has the link. They will not need a "
                "Gstore account, a Google account, or any sign-in at all."
            )
        )
        layout.addSpacing(6)
        layout.addWidget(
            _wrapped(
                "Treat the link like the file itself — whoever receives it can "
                "pass it on. You can stop sharing at any time, but that cannot "
                "take back a copy someone has already downloaded.",
                muted=True,
            )
        )
        layout.addSpacing(10)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Create link")
        ok.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.setLayout(layout)


class ShareLinkDialog(QDialog):
    """Show a live link, offer to copy it, and offer to withdraw it."""

    def __init__(self, name: str, link: str, part_count: int, shared_at=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Share link")
        self.setMinimumWidth(560)
        self.stop_requested = False

        layout = QVBoxLayout()
        layout.addWidget(_wrapped(f"<b>{name}</b> is shared."))

        detail = f"{part_count} piece{'s' if part_count != 1 else ''}"
        if shared_at:
            detail += f" · shared {shared_at}"
        layout.addWidget(_wrapped(detail, muted=True))
        layout.addSpacing(8)

        self.link_field = QLineEdit(link)
        self.link_field.setReadOnly(True)
        self.link_field.setCursorPosition(0)
        # Monospace: the link is a key, and a reader needs to be able to tell
        # one character from another when checking a pasted copy.
        self.link_field.setStyleSheet("font-family: Menlo, Consolas, monospace;")

        self.copy_button = QPushButton("Copy")
        self.copy_button.setDefault(True)
        self.copy_button.clicked.connect(self._copy)

        link_row = QHBoxLayout()
        link_row.addWidget(self.link_field)
        link_row.addWidget(self.copy_button)
        layout.addLayout(link_row)

        self.hint = _wrapped(
            "Anyone with this link can download the file, with no account. "
            "The part after the # never reaches any website.",
            muted=True,
        )
        layout.addWidget(self.hint)
        layout.addSpacing(10)

        self.stop_button = QPushButton("Stop sharing")
        self.stop_button.setToolTip(
            "Make every piece private again and delete the link. Anyone who "
            "already downloaded the file keeps their copy."
        )
        self.stop_button.clicked.connect(self._stop)

        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.reject)

        footer = QHBoxLayout()
        footer.addWidget(self.stop_button)
        footer.addStretch()
        footer.addWidget(close)
        layout.addLayout(footer)

        self.setLayout(layout)
        self.link_field.setFocus()
        self.link_field.selectAll()

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.link_field.text())
        self.copy_button.setText("Copied")
        self.hint.setText("Copied to the clipboard — paste it wherever you like.")
        self.hint.setStyleSheet(_MUTED)

    def _stop(self) -> None:
        self.stop_requested = True
        self.accept()


class OpenShareDialog(QDialog):
    """Ask for a link someone sent, prefilled if the clipboard holds one."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Open a share link")
        self.setMinimumWidth(560)

        layout = QVBoxLayout()
        layout.addWidget(_wrapped("Paste a Gstore share link:"))

        self.link_field = QLineEdit()
        self.link_field.setPlaceholderText("https://gstore.iampkb.dev/s#…")
        self.link_field.setStyleSheet("font-family: Menlo, Consolas, monospace;")
        self.link_field.textChanged.connect(self._revalidate)
        layout.addWidget(self.link_field)

        self.hint = _wrapped(
            "You do not need to have the sender's accounts linked.", muted=True
        )
        layout.addWidget(self.hint)
        layout.addSpacing(8)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok
        )
        self.ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Open")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.setLayout(layout)

        # Most people arrive here having just copied the link.
        clipboard = QGuiApplication.clipboard().text().strip()
        if clipboard and looks_like_link(clipboard):
            self.link_field.setText(clipboard)
            self.hint.setText("Found a share link on the clipboard.")
            self.hint.setStyleSheet(_MUTED)

        self._revalidate()
        self.link_field.setFocus()

    def _revalidate(self) -> None:
        # Checked as they type, so a mangled link is caught here rather than
        # after a network round trip.
        self.ok.setEnabled(looks_like_link(self.link_field.text()))

    def link(self) -> str:
        return self.link_field.text().strip()
