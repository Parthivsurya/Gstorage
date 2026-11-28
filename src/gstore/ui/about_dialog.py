"""What Gstore is, who wrote it, and what it does not promise.

Reached from the "?" at the bottom right of the main window. Everything in
here already exists somewhere — the licence, the privacy policy, the release
notes — but all of it lives on a website, and the person deciding whether to
hand this app their Google account is looking at the app.

Gstore is unsigned, so the operating system has already warned about it; the
name is one nobody recognises; and it asks for access to someone's Drive.
Saying plainly who made it and what it will not do for you is worth more
here than anything else this window could contain.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from gstore import __version__

SITE = "https://gstore.iampkb.dev"
REPO = "https://github.com/Parthivsurya/Gstorage"

# Not "open source": the licence reserves all rights and grants no permission
# to copy, modify or redistribute. "Free to use" is what is actually true.
EDITION = "Free to use"

# The specific risk, not a generic warranty paragraph. Gstore's index is the
# only record of which account each piece of a file lives on, and a user who
# loses it has files scattered across accounts with nothing to reassemble
# them. Pointing at the fix is more use than a wall of capitals.
DISCLAIMER = (
    "Gstore keeps the only record of which account each file lives on. "
    "Back up the index from the Storage tab, and keep that backup "
    "somewhere other than the machine running Gstore.\n\n"
    "This program is provided as is, with no warranty of any kind. The "
    "author is not liable for data loss, lost access to files, or any "
    "other loss arising from its use."
)


def _logo() -> QPixmap | None:
    """The app logo, whether running from source or from a bundle."""
    here = Path(__file__).resolve()
    candidates = [
        here.parent / "gstore_logo_120.png",          # bundled beside the UI
        here.parents[3] / "assets" / "gstore_logo_120.png",  # a source checkout
    ]
    for path in candidates:
        if path.is_file():
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                return pixmap
    return None


class AboutDialog(QDialog):
    """A short, honest summary of what this is."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Gstore")
        self.setModal(True)

        heading = QVBoxLayout()
        heading.addStretch()
        for text, style in (
            (f"Gstore {__version__}", "font-size: 16px; font-weight: 600;"),
            ("Copyright © 2026 PKB. All rights reserved.", "color: #666;"),
            (EDITION, "color: #666;"),
        ):
            label = QLabel(text)
            label.setStyleSheet(style)
            label.setTextFormat(Qt.TextFormat.PlainText)
            heading.addWidget(label)

        links = QLabel(
            f'<a href="{SITE}">Website</a> &nbsp;·&nbsp; '
            f'<a href="{SITE}/PRIVACY">Privacy policy</a> &nbsp;·&nbsp; '
            f'<a href="{SITE}/release-notes/">Release notes</a><br>'
            f'<a href="{REPO}">Source</a> &nbsp;·&nbsp; '
            f'<a href="{REPO}/blob/main/LICENSE">Licence</a> &nbsp;·&nbsp; '
            f'<a href="{REPO}/issues">Report an issue</a>'
        )
        links.setOpenExternalLinks(False)
        links.linkActivated.connect(webbrowser.open)
        heading.addWidget(links)
        heading.addStretch()

        top = QHBoxLayout()
        pixmap = _logo()
        if pixmap is not None:
            icon = QLabel()
            icon.setPixmap(
                pixmap.scaled(
                    96, 96,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            icon.setAlignment(Qt.AlignmentFlag.AlignTop)
            top.addWidget(icon)
            top.addSpacing(16)
        top.addLayout(heading)
        top.addStretch()

        summary = QLabel(
            "Gstore pools several of your own Google accounts into one "
            "virtual drive. Files move directly between your machine and "
            "Google; there is no server in between and nothing of yours "
            "reaches the author."
        )
        summary.setWordWrap(True)

        disclaimer = QLabel(DISCLAIMER)
        disclaimer.setWordWrap(True)
        disclaimer.setStyleSheet("color: #666;")
        disclaimer.setTextFormat(Qt.TextFormat.PlainText)

        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(close)

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(14)
        layout.addLayout(top)
        layout.addWidget(summary)
        layout.addWidget(disclaimer)
        layout.addStretch()
        layout.addLayout(buttons)
        self.setLayout(layout)
        self.setMinimumWidth(520)
