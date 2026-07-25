"""Files tab: the unified browser.

Renders one virtual tree from the index regardless of which account each file
physically lives on. In Phase 3 files appear here once uploaded (Phase 4); the
folder structure is fully interactive now.
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
from gstore.index import FileEntry, Folder, IndexStore
from gstore.ui.upload_worker import UploadWorker

# QListWidgetItem UserRole payload: (kind, id)
_FOLDER = "folder"
_FILE = "file"


class FileBrowser(QWidget):
    # Emitted after a successful upload so the storage dashboard can refresh.
    uploaded = Signal()

    def __init__(self, index: IndexStore) -> None:
        super().__init__()
        self.index = index
        self.current_parent_id: int | None = None  # None == virtual root
        self._upload_worker: UploadWorker | None = None

        self.breadcrumb = QHBoxLayout()
        self.breadcrumb.setSpacing(2)
        breadcrumb_box = QWidget()
        breadcrumb_box.setLayout(self.breadcrumb)

        self.entries = QListWidget()
        self.entries.itemDoubleClicked.connect(self._on_double_click)
        self.entries.currentItemChanged.connect(self._update_buttons)

        self.upload_button = QPushButton("Upload file")
        self.upload_button.clicked.connect(self._on_upload)
        self.new_folder_button = QPushButton("New folder")
        self.new_folder_button.clicked.connect(self._on_new_folder)
        self.rename_button = QPushButton("Rename")
        self.rename_button.clicked.connect(self._on_rename)
        self.delete_button = QPushButton("Delete")
        self.delete_button.clicked.connect(self._on_delete)

        buttons = QHBoxLayout()
        buttons.addWidget(self.upload_button)
        buttons.addWidget(self.new_folder_button)
        buttons.addWidget(self.rename_button)
        buttons.addWidget(self.delete_button)
        buttons.addStretch()

        self.empty_hint = QLabel("This folder is empty.")
        self.empty_hint.setStyleSheet("color: #888;")

        self.upload_bar = QProgressBar()
        self.upload_bar.setVisible(False)
        self.upload_status = QLabel("")
        self.upload_status.setStyleSheet("color: #888;")

        layout = QVBoxLayout()
        layout.addWidget(breadcrumb_box)
        layout.addWidget(self.entries)
        layout.addWidget(self.empty_hint)
        layout.addLayout(buttons)
        layout.addWidget(self.upload_bar)
        layout.addWidget(self.upload_status)
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
        where = entry.home_account_email or "unassigned"
        return f"📄  {entry.name}    {human_bytes(entry.size_bytes)} · {where}"

    def _add_item(self, text: str, kind: str, item_id: int) -> None:
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, (kind, item_id))
        self.entries.addItem(item)

    def _on_double_click(self, item: QListWidgetItem) -> None:
        kind, item_id = item.data(Qt.ItemDataRole.UserRole)
        if kind == _FOLDER:
            self.navigate_to(item_id)

    # -- upload ---------------------------------------------------------------

    def _on_upload(self) -> None:
        if self._upload_worker is not None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file to upload")
        if not path:
            return

        self.upload_button.setEnabled(False)
        self.upload_bar.setRange(0, 100)
        self.upload_bar.setValue(0)
        self.upload_bar.setVisible(True)
        self.upload_status.setText("Uploading…")

        worker = UploadWorker(path, self.current_parent_id, self)
        worker.progress.connect(self._on_upload_progress)
        worker.succeeded.connect(self._on_upload_succeeded)
        worker.failed.connect(self._on_upload_failed)
        worker.finished.connect(self._on_upload_finished)
        self._upload_worker = worker
        worker.start()

    def _on_upload_progress(self, fraction: float) -> None:
        self.upload_bar.setValue(int(fraction * 100))

    def _on_upload_succeeded(self, name: str, email: str) -> None:
        self.upload_status.setText(f"Uploaded {name} → {email}")
        self.refresh()
        self.uploaded.emit()

    def _on_upload_failed(self, message: str) -> None:
        self.upload_status.setText("")
        QMessageBox.warning(self, "Upload failed", message)

    def _on_upload_finished(self) -> None:
        self._upload_worker = None
        self.upload_button.setEnabled(True)
        self.upload_bar.setVisible(False)

    # -- actions --------------------------------------------------------------

    def _selected(self) -> tuple[str, int] | None:
        item = self.entries.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _update_buttons(self) -> None:
        has_selection = self._selected() is not None
        self.rename_button.setEnabled(has_selection)
        self.delete_button.setEnabled(has_selection)

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
        name, ok = QInputDialog.getText(
            self, "Rename", "New name:", text=current.name
        )
        name = name.strip()
        if ok and name:
            if kind == _FOLDER:
                self.index.rename_folder(item_id, name)
            else:
                self.index.rename_file(item_id, name)
            self.refresh()

    def _on_delete(self) -> None:
        selection = self._selected()
        if selection is None:
            return
        kind, item_id = selection
        if kind == _FOLDER:
            message = (
                "Delete this folder and everything inside it from Gstore?\n\n"
                "This removes index entries only."
            )
        else:
            message = (
                "Remove this file from Gstore?\n\n"
                "This removes it from the index only; the copy in Google Drive "
                "is deleted in a later phase."
            )
        if (
            QMessageBox.question(self, "Delete", message)
            == QMessageBox.StandardButton.Yes
        ):
            if kind == _FOLDER:
                self.index.delete_folder(item_id)
            else:
                self.index.delete_file(item_id)
            self.refresh()
