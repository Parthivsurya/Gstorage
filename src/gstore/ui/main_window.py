"""Main application window — tabbed shell.

Files tab: the unified browser over the metadata index (Phase 3).
Storage tab: combined-usage dashboard + account management (Phase 2).
"""

from __future__ import annotations

from PySide6.QtWidgets import QMainWindow, QTabWidget

from gstore import __version__
from gstore.db import Database
from gstore.index import IndexStore
from gstore.ui.file_browser import FileBrowser
from gstore.ui.storage_panel import StoragePanel


class MainWindow(QMainWindow):
    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

        self.setWindowTitle(f"Gstore {__version__}")
        self.resize(820, 600)

        self.file_browser = FileBrowser(IndexStore(db))
        self.storage_panel = StoragePanel(db)

        # An upload changes account usage; refresh the dashboard when it lands.
        self.file_browser.uploaded.connect(self.storage_panel.reload)

        tabs = QTabWidget()
        tabs.addTab(self.file_browser, "Files")
        tabs.addTab(self.storage_panel, "Storage")
        self.setCentralWidget(tabs)
