"""Main application window — tabbed shell.

Files tab: the unified browser over the metadata index (Phase 3).
Storage tab: combined-usage dashboard + account management (Phase 2).
"""

from __future__ import annotations

import webbrowser

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gstore import __version__, config
from gstore.db import Database
from gstore.index import IndexStore
from gstore.ui.file_browser import FileBrowser
from gstore.ui.storage_panel import StoragePanel
from gstore.ui.update_worker import UpdateWorker


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

        self._build_update_banner()

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.update_banner)
        layout.addWidget(tabs)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self._update_worker: UpdateWorker | None = None
        self._pending_release = None
        self._start_update_check()

    # -- update notice --------------------------------------------------------

    def _build_update_banner(self) -> None:
        """A strip above the tabs, hidden until there is something to report.

        A banner rather than a dialog on launch: an update is worth mentioning,
        not worth blocking the app behind. Nothing is downloaded or installed
        automatically — the buttons only open a browser.
        """
        self.update_banner = QFrame()
        self.update_banner.setVisible(False)
        self.update_banner.setFrameShape(QFrame.Shape.StyledPanel)
        self.update_banner.setStyleSheet(
            "QFrame { background: #2d4f7c; border: none; }"
            "QLabel { color: white; }"
        )

        self.update_label = QLabel("")
        self.update_label.setTextFormat(Qt.TextFormat.PlainText)

        self.update_details_button = QPushButton("What's new")
        self.update_details_button.clicked.connect(self._on_update_details)
        self.update_get_button = QPushButton("Download")
        self.update_get_button.clicked.connect(self._on_update_download)
        self.update_later_button = QPushButton("Later")
        self.update_later_button.setToolTip("Hide until the next launch.")
        self.update_later_button.clicked.connect(
            lambda: self.update_banner.setVisible(False)
        )
        self.update_skip_button = QPushButton("Skip this version")
        self.update_skip_button.setToolTip(
            "Do not mention this version again. Later versions still will."
        )
        self.update_skip_button.clicked.connect(self._on_update_skip)

        row = QHBoxLayout()
        row.setContentsMargins(12, 8, 12, 8)
        row.addWidget(self.update_label)
        row.addStretch()
        for button in (
            self.update_details_button,
            self.update_get_button,
            self.update_later_button,
            self.update_skip_button,
        ):
            row.addWidget(button)
        self.update_banner.setLayout(row)

    def _start_update_check(self) -> None:
        worker = UpdateWorker(__version__, self)
        worker.update_available.connect(self._on_update_available)
        worker.finished.connect(self._on_update_check_finished)
        self._update_worker = worker
        worker.start()

    def _on_update_check_finished(self) -> None:
        self._update_worker = None

    def _on_update_available(self, release) -> None:
        self._pending_release = release
        self.update_label.setText(
            f"Gstore {release.version} is available — you have {__version__}."
        )
        self.update_banner.setVisible(True)

    def _on_update_details(self) -> None:
        release = self._pending_release
        if release is None:
            return
        notes = release.notes or "No release notes were published."
        if len(notes) > 2000:
            notes = notes[:2000] + "\n\n…"
        QMessageBox.information(self, f"Gstore {release.version}", notes)

    def _on_update_download(self) -> None:
        release = self._pending_release
        if release is None:
            return
        # Opens the release page; Gstore never downloads or installs anything
        # on its own. Unsigned builds should be fetched deliberately.
        webbrowser.open(release.url)

    def _on_update_skip(self) -> None:
        release = self._pending_release
        if release is not None:
            config.skip_version(release.version)
        self.update_banner.setVisible(False)

    # -- shutdown -------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """Stop the tabs' background threads before the widget tree is torn down.

        Qt delivers QCloseEvent only to top-level windows, so the tabs never
        get one of their own — without this, a running upload QThread is
        destroyed mid-flight and the process aborts rather than quitting.
        """
        if not self.file_browser.prepare_to_close():
            event.ignore()
            return
        worker = self._update_worker
        if worker is not None and worker.isRunning():
            worker.wait(2000)  # a bounded GET; it will be done or timed out
        super().closeEvent(event)
