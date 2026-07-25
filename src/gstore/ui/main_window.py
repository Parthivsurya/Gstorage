"""Main application window.

Phase 0: a placeholder shell that confirms the window renders and that the
local index opened successfully. Real dashboard/browser widgets land in later
phases (combined storage view in Phase 2, unified browser in Phase 3).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QVBoxLayout, QWidget

from gstore import __version__
from gstore.db import Database


class MainWindow(QMainWindow):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

        self.setWindowTitle(f"Gstore {__version__}")
        self.resize(720, 480)

        title = QLabel("Gstore")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 28px; font-weight: 600;")

        subtitle = QLabel("Multi-account Google Drive aggregator")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("color: #888;")

        status = QLabel(
            f"Local index ready (schema v{self.db.schema_version()})\n"
            f"{self.db.path}"
        )
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status.setStyleSheet("color: #4c9a4c; margin-top: 24px;")

        layout = QVBoxLayout()
        layout.addStretch()
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addWidget(status)
        layout.addStretch()

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
