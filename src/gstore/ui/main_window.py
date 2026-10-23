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

        # Uploads/deletes change account usage; refresh the dashboard.
        self.file_browser.changed.connect(self.storage_panel.reload)
        # Reconcile (repair) can prune/recover files; refresh the browser.
        self.storage_panel.index_changed.connect(self.file_browser.refresh)

        tabs = QTabWidget()
        tabs.addTab(self.file_browser, "Files")
        tabs.addTab(self.storage_panel, "Storage")
        self.setCentralWidget(tabs)

    def closeEvent(self, event) -> None:
        """Stop the tabs' background threads before the widget tree is torn down.

        Qt delivers QCloseEvent only to top-level windows, so the tabs never
        get one of their own — without this, a running upload QThread is
        destroyed mid-flight and the process aborts rather than quitting.
        """
        if not self.file_browser.prepare_to_close():
            event.ignore()
            return
        super().closeEvent(event)
