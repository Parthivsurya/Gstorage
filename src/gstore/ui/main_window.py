"""Main application window — tabbed shell.

Files tab: the unified browser over the metadata index (Phase 3).
Storage tab: combined-usage dashboard + account management (Phase 2).
"""

from __future__ import annotations

import subprocess
import sys
import webbrowser
from pathlib import Path

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
from gstore.ui.about_dialog import AboutDialog
from gstore.ui.file_browser import FileBrowser
from gstore.ui.storage_panel import StoragePanel
from gstore.ui.update_download_worker import UpdateDownloadWorker
from gstore.ui.update_progress_dialog import UpdateProgressDialog
from gstore.ui.update_worker import UpdateWorker
from gstore.updater.download import asset_name
from gstore.updater.install import InstallError, begin_install, can_install


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

        # A "?" in the corner rather than a menu item: the app is unsigned and
        # the name is unfamiliar, so what it is and who wrote it should be one
        # click away from the window itself, not only on a website.
        self.about_button = QPushButton("?")
        self.about_button.setFixedSize(28, 28)
        self.about_button.setToolTip("About Gstore")
        self.about_button.clicked.connect(self._on_about)
        footer = QHBoxLayout()
        footer.setContentsMargins(12, 0, 12, 8)
        footer.addStretch()
        footer.addWidget(self.about_button)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.update_banner)
        layout.addWidget(tabs)
        layout.addLayout(footer)
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        self._update_worker: UpdateWorker | None = None
        self._update_download: UpdateDownloadWorker | None = None
        self._update_progress: UpdateProgressDialog | None = None
        # Set once an update helper is waiting on this process to exit,
        # so the close path does not ask about transfers a second time.
        self._installing = False
        self._pending_release = None
        self._start_update_check()

    def _on_about(self) -> None:
        AboutDialog(self).exec()

    # -- update notice --------------------------------------------------------

    def _build_update_banner(self) -> None:
        """A strip above the tabs, hidden until there is something to report.

        A banner rather than a dialog on launch: an update is worth mentioning,
        not worth blocking the app behind. Nothing happens without the user
        pressing something, and nothing is ever installed automatically: the
        most Gstore will do is fetch the right file, prove it is genuine, and
        show it to them.
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
        self.update_get_button.setToolTip(
            "Fetch the build for this machine and check it was signed by "
            "Gstore, then show you the file. Nothing is installed for you."
        )
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
        """Fetch this platform's build, verify its signature, and reveal it.

        Picking the right file by hand is a real way to go wrong — three
        builds are listed and the macOS one is Apple silicon only — and an
        unsigned download has nothing vouching for it until this checks.
        Installing is still the user's move; see gstore.updater.download for
        why replacing a running app is left out.
        """
        release = self._pending_release
        if release is None or self._update_download is not None:
            return

        if asset_name() is None or not release.assets:
            # No build for this machine, or a release published before
            # signing existed. The page is the honest fallback.
            webbrowser.open(release.url)
            return

        self.update_get_button.setEnabled(False)

        dialog = UpdateProgressDialog(release.version, self)
        self._update_progress = dialog

        worker = UpdateDownloadWorker(release, config.data_dir() / "updates", self)
        worker.progress.connect(self._on_update_progress)
        # Cancelling the dialog stops the worker; the worker finishing closes
        # the dialog. Either way only one of them ends the pair.
        dialog.rejected.connect(worker.requestInterruption)
        dialog.cancel.clicked.connect(worker.requestInterruption)
        worker.succeeded.connect(self._on_update_downloaded)
        worker.failed.connect(self._on_update_download_failed)
        worker.rejected.connect(self._on_update_rejected)
        worker.cancelled.connect(self._on_update_cancelled)
        worker.finished.connect(self._on_update_download_finished)
        self._update_download = worker
        worker.start()
        dialog.show()

    def _on_update_progress(self, read: int, total: int) -> None:
        if self._update_progress is not None:
            self._update_progress.on_progress(read, total)

    def _close_update_progress(self) -> None:
        dialog = self._update_progress
        self._update_progress = None
        if dialog is not None:
            dialog.close()

    def _on_update_downloaded(self, path: str) -> None:
        self._close_update_progress()
        self.update_label.setText("Downloaded and verified.")
        version = self._pending_release.version
        installable, why = can_install()

        if not installable:
            # Running from source, or installed somewhere this user cannot
            # write. Showing them the file is the honest fallback.
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setText(
                f"Update  {__version__} \u2192 {version}  is ready to install."
            )
            box.setInformativeText(f"Open the file to install it.\n\n{why}")
            box.setDetailedText(str(path))
            box.exec()
            self._reveal(path)
            return

        # Short on purpose. The signature check was shown while it happened,
        # and this is a routine action a user will take every few weeks —
        # three paragraphs of reassurance reads as a warning the second time.
        ask = QMessageBox(self)
        ask.setIcon(QMessageBox.Icon.Question)
        # Both numbers, not just the new one: "0.1.7 -> 0.1.8" says what is
        # being left behind as well as what arrives, which is the thing
        # someone deciding whether to update actually wants to see.
        ask.setText(f"Install update  {__version__} \u2192 {version}?")
        ask.setInformativeText("Gstore will quit and start again.")
        install = ask.addButton("Install", QMessageBox.ButtonRole.AcceptRole)
        ask.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
        ask.setDefaultButton(install)
        ask.exec()

        if ask.clickedButton() is not install:
            self._reveal(path)
            return
        self._install_and_quit(Path(path), version)

    def _install_and_quit(self, archive: Path, version: str) -> None:
        """Stage the update, start the helper, then quit so it can proceed.

        The order is the whole point. A transfer still running is asked about
        *before* anything is staged, because the helper waits on this
        process's id — start it and then stay open, and it waits for an exit
        that never comes.
        """
        if not self.file_browser.prepare_to_close():
            self.update_label.setText("Install cancelled; the file is saved.")
            return

        self.update_label.setText(f"Installing Gstore {version}…")
        try:
            begin_install(archive, config.data_dir() / "updates")
        except InstallError as exc:
            QMessageBox.warning(
                self,
                "Could not install the update",
                f"{exc}\n\nGstore has not been changed. The downloaded file "
                f"is still at:\n{archive}",
            )
            self.update_label.setText("Install failed; the file is saved.")
            return

        # The helper is now waiting for this process to exit. Quitting is
        # what lets it start, and close() runs the normal shutdown so no
        # thread is destroyed mid-flight.
        self._installing = True
        self.close()

    def _on_update_cancelled(self) -> None:
        self._close_update_progress()
        self.update_label.setText("Download stopped.")

    def _on_update_rejected(self, message: str) -> None:
        self._close_update_progress()
        self.update_label.setText("That download could not be verified.")
        QMessageBox.critical(self, "Update not verified", message)

    def _on_update_download_failed(self, message: str) -> None:
        self._close_update_progress()
        self.update_label.setText("The download did not finish.")
        QMessageBox.warning(
            self,
            "Download failed",
            f"{message}\n\nYou can download it from the releases page "
            "instead.",
        )

    def _on_update_download_finished(self) -> None:
        self._close_update_progress()
        self._update_download = None
        self.update_get_button.setEnabled(True)

    @staticmethod
    def _reveal(path: str) -> None:
        """Show the downloaded file in the system file manager."""
        try:
            if sys.platform == "darwin":
                subprocess.run(["open", "-R", path], check=False)
            elif sys.platform.startswith("win"):
                subprocess.run(["explorer", "/select,", path], check=False)
            else:
                subprocess.run(
                    ["xdg-open", str(Path(path).parent)], check=False
                )
        except OSError:
            pass  # revealing is a convenience; the path is in the dialog

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
        if not self._installing and not self.file_browser.prepare_to_close():
            event.ignore()
            return
        worker = self._update_worker
        if worker is not None and worker.isRunning():
            worker.wait(2000)  # a bounded GET; it will be done or timed out
        super().closeEvent(event)
