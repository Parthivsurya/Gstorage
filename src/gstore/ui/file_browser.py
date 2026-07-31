"""Files tab: the unified browser.

Renders one virtual tree from the index regardless of which account each file
physically lives on, and drives the whole file lifecycle: upload (Phase 4),
download and delete (Phase 5).
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gstore.format import human_bytes
from gstore.index import FileEntry, IndexStore
from gstore.ui.delete_worker import DeleteWorker
from gstore.ui.download_worker import DownloadWorker
from gstore.ui.upload_worker import UploadWorker

# QListWidgetItem UserRole payload: (kind, id)
_FOLDER = "folder"
_FILE = "file"


class FileBrowser(QWidget):
    # Emitted after any change that affects account usage (upload / delete).
    changed = Signal()

    def __init__(self, index: IndexStore) -> None:
        super().__init__()
        self.index = index
        self.current_parent_id: int | None = None  # None == virtual root
        self._upload_worker: UploadWorker | None = None
        self._download_worker: DownloadWorker | None = None
        self._delete_worker: DeleteWorker | None = None

        self.breadcrumb = QHBoxLayout()
        self.breadcrumb.setSpacing(2)
        breadcrumb_box = QWidget()
        breadcrumb_box.setLayout(self.breadcrumb)

        self.entries = QListWidget()
        self.entries.itemDoubleClicked.connect(self._on_double_click)
        self.entries.currentItemChanged.connect(self._update_buttons)

        self.upload_button = QPushButton("Upload file")
        self.upload_button.clicked.connect(self._on_upload)
        self.download_button = QPushButton("Download")
        self.download_button.clicked.connect(self._on_download)
        self.new_folder_button = QPushButton("New folder")
        self.new_folder_button.clicked.connect(self._on_new_folder)
        self.rename_button = QPushButton("Rename")
        self.rename_button.clicked.connect(self._on_rename)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self._on_delete)

        buttons = QHBoxLayout()
        for button in (
            self.upload_button,
            self.download_button,
            self.new_folder_button,
            self.rename_button,
            self.delete_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch()

        self.empty_hint = QLabel("This folder is empty.")
        self.empty_hint.setStyleSheet("color: #888;")

        self.transfer_bar = QProgressBar()
        self.transfer_bar.setVisible(False)
        self.transfer_status = QLabel("")
        self.transfer_status.setStyleSheet("color: #888;")

        layout = QVBoxLayout()
        layout.addWidget(breadcrumb_box)
        layout.addWidget(self.entries)
        layout.addWidget(self.empty_hint)
        layout.addLayout(buttons)
        layout.addWidget(self.transfer_bar)
        layout.addWidget(self.transfer_status)
        self.setLayout(layout)

        self.refresh()

    # -- navigation -----------------------------------------------------------

    def navigate_to(self, parent_id: int | None) -> None:
        self.current_parent_id = parent_id
        self.refresh()

    def refresh(self) -> None:
        self._render_breadcrumb()
        self._render_entries()
        self._update_buttons()

    def _render_breadcrumb(self) -> None:
        while self.breadcrumb.count():
            item = self.breadcrumb.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        chain = self.index.folder_path(self.current_parent_id)
        self._add_crumb("Home", None)
        for folder in chain:
            sep = QLabel("›")
            sep.setStyleSheet("color: #888;")
            self.breadcrumb.addWidget(sep)
            self._add_crumb(folder.name, folder.id)
        self.breadcrumb.addStretch()

    def _add_crumb(self, label: str, folder_id: int | None) -> None:
        button = QPushButton(label)
        button.setFlat(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(lambda _=False, fid=folder_id: self.navigate_to(fid))
        is_current = folder_id == self.current_parent_id
        button.setStyleSheet(
            "text-align: left; border: none; padding: 2px 4px;"
            + ("font-weight: 600;" if is_current else "color: #6aa0ff;")
        )
        self.breadcrumb.addWidget(button)

    def _render_entries(self) -> None:
        self.entries.clear()
        folders = self.index.list_folders(self.current_parent_id)
        files = self.index.list_files(self.current_parent_id)

        for folder in folders:
            self._add_item(f"📁  {folder.name}", _FOLDER, folder.id)
        for entry in files:
            self._add_item(self._file_label(entry), _FILE, entry.id)

        self.empty_hint.setVisible(not folders and not files)

    def _file_label(self, entry: FileEntry) -> str:
        size = human_bytes(entry.size_bytes)
        if not entry.is_available:
            if entry.is_chunked:
                reason = "a part's account needs re-link"
            elif entry.home_account_id is not None:
                reason = "needs re-link"
            else:
                reason = "home account removed"
            return f"⚠  {entry.name}    {size} · unavailable ({reason})"
        if entry.is_chunked:
            return f"📄  {entry.name}    {size} · split into {entry.chunk_count} chunks"
        return f"📄  {entry.name}    {size} · {entry.home_account_email}"

    def _add_item(self, text: str, kind: str, item_id: int) -> None:
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, (kind, item_id))
        self.entries.addItem(item)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        kind, item_id = item.data(Qt.ItemDataRole.UserRole)
        if kind == _FOLDER:
            self.navigate_to(item_id)

    # -- selection / button state --------------------------------------------

    def _selected(self) -> tuple[str, int] | None:
        item = self.entries.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _busy(self) -> bool:
        return any(
            w is not None
            for w in (self._upload_worker, self._download_worker, self._delete_worker)
        )

    def _update_buttons(self) -> None:
        selection = self._selected()
        is_file = selection is not None and selection[0] == _FILE
        busy = self._busy()

        # Downloading needs the bytes; block it for files whose home account is
        # removed or revoked. Rename/delete stay allowed (index-only + best-effort
        # Drive delete), so unavailable files aren't stuck in the browser.
        downloadable = False
        if is_file:
            entry = self.index.get_file(selection[1])
            downloadable = entry is not None and entry.is_available

        self.upload_button.setEnabled(not busy)
        self.download_button.setEnabled(downloadable and not busy)
        self.rename_button.setEnabled(selection is not None and not busy)
        self.delete_button.setEnabled(selection is not None and not busy)

    def _begin_transfer(self, message: str, determinate: bool) -> None:
        self.transfer_bar.setRange(0, 100 if determinate else 0)
        self.transfer_bar.setValue(0)
        self.transfer_bar.setVisible(True)
        self.transfer_status.setText(message)
        self._update_buttons()

    def _on_transfer_progress(self, fraction: float) -> None:
        self.transfer_bar.setValue(int(fraction * 100))

    # -- upload ---------------------------------------------------------------

    def _on_upload(self) -> None:
        if self._busy():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file to upload")
        if not path:
            return
        self._begin_transfer("Uploading…", determinate=True)

        worker = UploadWorker(path, self.current_parent_id, self)
        worker.progress.connect(self._on_transfer_progress)
        worker.succeeded.connect(self._on_upload_succeeded)
        worker.failed.connect(self._on_upload_failed)
        worker.finished.connect(self._on_upload_finished)
        self._upload_worker = worker
        worker.start()

    def _on_upload_succeeded(self, name: str, email: str) -> None:
        self.transfer_status.setText(f"Uploaded {name} → {email}")
        self.refresh()
        self.changed.emit()

    def _on_upload_failed(self, message: str) -> None:
        self.transfer_status.setText("")
        QMessageBox.warning(self, "Upload failed", message)

    def _on_upload_finished(self) -> None:
        self._upload_worker = None
        self.transfer_bar.setVisible(False)
        self._update_buttons()

    # -- download -------------------------------------------------------------

    def _on_download(self) -> None:
        selection = self._selected()
        if selection is None or selection[0] != _FILE or self._busy():
            return
        entry = self.index.get_file(selection[1])
        if entry is None:
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Save file as", entry.name)
        if not dest:
            return
        self._begin_transfer("Downloading…", determinate=True)

        worker = DownloadWorker(entry.id, dest, self)
        worker.progress.connect(self._on_transfer_progress)
        worker.succeeded.connect(self._on_download_succeeded)
        worker.failed.connect(self._on_download_failed)
        worker.finished.connect(self._on_download_finished)
        self._download_worker = worker
        worker.start()

    def _on_download_succeeded(self, dest: str) -> None:
        self.transfer_status.setText(f"Saved to {dest}")

    def _on_download_failed(self, message: str) -> None:
        self.transfer_status.setText("")
        QMessageBox.warning(self, "Download failed", message)

    def _on_download_finished(self) -> None:
        self._download_worker = None
        self.transfer_bar.setVisible(False)
        self._update_buttons()

    # -- folder / rename ------------------------------------------------------

    def _on_new_folder(self) -> None:
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        name = name.strip()
        if ok and name:
            self.index.create_folder(name, self.current_parent_id)
            self.refresh()

    def _on_rename(self) -> None:
        selection = self._selected()
        if selection is None:
            return
        kind, item_id = selection
        current = (
            self.index.get_folder(item_id)
            if kind == _FOLDER
            else self.index.get_file(item_id)
        )
        if current is None:
            return
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=current.name)
        name = name.strip()
        if ok and name:
            if kind == _FOLDER:
                self.index.rename_folder(item_id, name)
            else:
                self.index.rename_file(item_id, name)
            self.refresh()

    # -- delete ---------------------------------------------------------------

    def _on_delete(self) -> None:
        selection = self._selected()
        if selection is None or self._busy():
            return
        kind, item_id = selection
        if kind == _FOLDER:
            message = (
                "Delete this folder and everything inside it?\n\n"
                "Files are removed from Google Drive and from Gstore."
            )
        else:
            message = (
                "Delete this file?\n\n"
                "It is removed from Google Drive and from Gstore."
            )
        if (
            QMessageBox.question(self, "Delete", message)
            != QMessageBox.StandardButton.Yes
        ):
            return

        self._begin_transfer("Deleting…", determinate=False)
        worker = DeleteWorker(kind, item_id, self)
        worker.succeeded.connect(self._on_delete_succeeded)
        worker.failed.connect(self._on_delete_failed)
        worker.finished.connect(self._on_delete_finished)
        self._delete_worker = worker
        worker.start()

    def _on_delete_succeeded(self, failures: int) -> None:
        if failures:
            self.transfer_status.setText(
                f"Deleted, but {failures} Drive file(s) could not be removed "
                "(account offline or revoked). Kept for retry."
            )
        else:
            self.transfer_status.setText("Deleted.")
        self.refresh()
        self.changed.emit()

    def _on_delete_failed(self, message: str) -> None:
        self.transfer_status.setText("")
        QMessageBox.warning(self, "Delete failed", message)

    def _on_delete_finished(self) -> None:
        self._delete_worker = None
        self.transfer_bar.setVisible(False)
        self._update_buttons()
