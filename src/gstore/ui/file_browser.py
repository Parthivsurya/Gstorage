"""Files tab: the unified browser.

Renders one virtual tree from the index regardless of which account each file
physically lives on, and drives the whole file lifecycle: upload (Phase 4),
download and delete (Phase 5). "Scan Drive now" adds a live, read-only view that
lists every file across all linked accounts straight from Google, independent of
Gstore's index.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from gstore import config
from gstore.format import human_bytes, human_duration, human_rate
from gstore.index import FileEntry, IndexStore
from gstore.ui.delete_worker import DeleteWorker
from gstore.ui.download_worker import DownloadWorker
from gstore.ui.drive_download_worker import DriveDownloadWorker
from gstore.ui.drive_scan_worker import DriveScanWorker, ScanResult, ScanRow
from gstore.ui.drive_trash_worker import DriveTrashWorker
from gstore.ui.thumbnail_worker import ThumbnailWorker
from gstore.ui.upload_worker import UploadWorker

# Trailing window for the speed estimate. Long enough to smooth out
# per-chunk jitter, short enough to reflect the connection now.
_RATE_WINDOW_SEC = 30.0

# QListWidgetItem UserRole payload: (kind, id). For _DRIVE, id is the index of
# the ScanRow within the current (search-filtered) live-scan result.
_FOLDER = "folder"
_FILE = "file"
_DRIVE = "drivescan"

# Second data role: the Drive file id an item maps to, for thumbnail delivery.
_ROLE_DRIVE_ID = Qt.ItemDataRole.UserRole + 1

# View modes for the entries list. LIST = detailed rows; GRID = wrapping icons
# with names; GALLERY = large icons that fill in with Drive thumbnails.
_VIEW_LIST = "list"
_VIEW_GRID = "grid"
_VIEW_GALLERY = "gallery"

# (icon px, grid cell) per view mode for the QListWidget in IconMode.
_GRID_SIZES = {
    _VIEW_GRID: (48, QSize(110, 78)),
    _VIEW_GALLERY: (140, QSize(180, 168)),
}


class FileBrowser(QWidget):
    # Emitted after any change that affects account usage (upload / delete).
    changed = Signal()

    def __init__(self, index: IndexStore) -> None:
        super().__init__()
        self.index = index
        self.current_parent_id: int | None = None  # None == virtual root
        self._query = ""  # non-empty == flat search across the whole tree
        # Live Drive scan: None == browsing the index; a ScanResult == showing
        # the combined read-only listing pulled straight from every account.
        self._scan: ScanResult | None = None
        self._visible_rows: list[ScanRow] = []  # rows shown after search filter
        self._upload_worker: UploadWorker | None = None
        self._transfer_message = ""
        self._transfer_total: int | None = None
        # Recent (monotonic time, fraction) samples, for a *current* speed
        # rather than an average over the whole run.
        self._rate_samples: deque[tuple[float, float]] = deque()
        self._download_worker: DownloadWorker | None = None
        self._delete_worker: DeleteWorker | None = None
        self._scan_worker: DriveScanWorker | None = None
        self._drive_download_worker: DriveDownloadWorker | None = None
        self._drive_trash_worker: DriveTrashWorker | None = None
        self._trashing: ScanRow | None = None  # row being moved to Trash

        self._view_mode = _VIEW_LIST
        # Fetched Drive thumbnails, keyed by Drive file id, reused across renders.
        self._thumb_cache: dict[str, QPixmap] = {}
        self._thumb_worker: ThumbnailWorker | None = None
        self._folder_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self._file_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search all files…")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.textChanged.connect(self._on_search_changed)
        self.view_switch = self._build_view_switch()

        top_row = QHBoxLayout()
        top_row.addWidget(self.search_box)
        top_row.addLayout(self.view_switch)

        self.breadcrumb = QHBoxLayout()
        self.breadcrumb.setSpacing(2)
        breadcrumb_box = QWidget()
        breadcrumb_box.setLayout(self.breadcrumb)

        self.entries = QListWidget()
        self.entries.itemDoubleClicked.connect(self._on_double_click)
        self.entries.currentItemChanged.connect(self._update_buttons)
        self._apply_view_mode()

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
        self.scan_button = QPushButton("Scan Drive now")
        self.scan_button.setToolTip(
            "List every file across all linked accounts, live from Google Drive."
        )
        self.scan_button.clicked.connect(self._on_scan_toggle)

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
        buttons.addWidget(self.scan_button)

        self.empty_hint = QLabel("This folder is empty.")
        self.empty_hint.setStyleSheet("color: #888;")

        self.transfer_bar = QProgressBar()
        self.transfer_bar.setVisible(False)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setVisible(False)
        self.cancel_button.setToolTip(
            "Stop the upload. What has already been uploaded is kept — choose "
            "the same file again to carry on from here."
        )
        self.cancel_button.clicked.connect(self._on_cancel_upload)
        transfer_row = QHBoxLayout()
        transfer_row.addWidget(self.transfer_bar)
        transfer_row.addWidget(self.cancel_button)

        self.transfer_status = QLabel("")
        self.transfer_status.setStyleSheet("color: #888;")

        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addWidget(breadcrumb_box)
        layout.addWidget(self.entries)
        layout.addWidget(self.empty_hint)
        layout.addLayout(buttons)
        layout.addLayout(transfer_row)
        layout.addWidget(self.transfer_status)
        self.setLayout(layout)

        self.refresh()

    # -- view modes -----------------------------------------------------------

    def _build_view_switch(self) -> QHBoxLayout:
        """Segmented Grid / List / Gallery control (icons via unicode glyphs)."""
        row = QHBoxLayout()
        row.setSpacing(0)
        self._view_group = QButtonGroup(self)
        specs = [
            (_VIEW_GRID, "▦", "Grid — thumbnails in a wrapping grid"),
            (_VIEW_LIST, "≣", "List — one detailed row per file"),
            (_VIEW_GALLERY, "🖾", "Gallery — large Drive previews"),
        ]
        for mode, glyph, tip in specs:
            button = QPushButton(glyph)
            button.setCheckable(True)
            button.setToolTip(tip)
            button.setFixedWidth(36)
            button.setChecked(mode == self._view_mode)
            button.clicked.connect(lambda _=False, m=mode: self._set_view_mode(m))
            self._view_group.addButton(button)
            row.addWidget(button)
        return row

    def _set_view_mode(self, mode: str) -> None:
        if mode == self._view_mode:
            return
        self._view_mode = mode
        self._apply_view_mode()
        self.refresh()

    def _apply_view_mode(self) -> None:
        if self._view_mode == _VIEW_LIST:
            self.entries.setViewMode(QListView.ViewMode.ListMode)
            self.entries.setIconSize(QSize(16, 16))
            self.entries.setGridSize(QSize())  # reset → natural row height
            self.entries.setWordWrap(False)
            self.entries.setSpacing(0)
            return
        icon_px, cell = _GRID_SIZES[self._view_mode]
        self.entries.setViewMode(QListView.ViewMode.IconMode)
        self.entries.setIconSize(QSize(icon_px, icon_px))
        self.entries.setGridSize(cell)
        self.entries.setResizeMode(QListView.ResizeMode.Adjust)
        self.entries.setMovement(QListView.Movement.Static)
        self.entries.setWordWrap(True)
        self.entries.setSpacing(6)

    @property
    def _icon_view(self) -> bool:
        return self._view_mode != _VIEW_LIST

    # -- navigation -----------------------------------------------------------

    def navigate_to(self, parent_id: int | None) -> None:
        # Jumping to a folder leaves search mode so the folder's contents show.
        if self._query:
            self.search_box.clear()  # triggers _on_search_changed → refresh
        self.current_parent_id = parent_id
        self.refresh()

    @property
    def _searching(self) -> bool:
        return bool(self._query)

    @property
    def _scanning(self) -> bool:
        return self._scan is not None

    def _on_search_changed(self, text: str) -> None:
        self._query = text.strip()
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

        # In scan / search mode the breadcrumb is meaningless (results span
        # folders and accounts); show a summary line instead.
        if self._scanning:
            self.breadcrumb.addWidget(self._scan_summary_label())
            self.breadcrumb.addStretch()
            return
        if self._searching:
            label = QLabel("Search results — across all folders and accounts")
            label.setStyleSheet("color: #888;")
            self.breadcrumb.addWidget(label)
            self.breadcrumb.addStretch()
            return

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

        if self._scanning:
            self._render_scan_rows()
            return

        if self._searching:
            matches = self.index.search_files(self._query)
            for entry in matches:
                label = f"{self._file_label(entry)}    —  in {self._location(entry)}"
                self._add_entry(label, entry.name, _FILE, entry.id, is_folder=False)
            self.empty_hint.setText(f"No files match “{self._query}”.")
            self.empty_hint.setVisible(not matches)
            return

        folders = self.index.list_folders(self.current_parent_id)
        files = self.index.list_files(self.current_parent_id)

        for folder in folders:
            self._add_entry(
                f"📁  {folder.name}", folder.name, _FOLDER, folder.id, is_folder=True
            )
        for entry in files:
            self._add_entry(
                self._file_label(entry), entry.name, _FILE, entry.id, is_folder=False
            )

        self.empty_hint.setText("This folder is empty.")
        self.empty_hint.setVisible(not folders and not files)

    def _location(self, entry: FileEntry) -> str:
        """Human path of the folder a file lives in, for search results."""
        chain = self.index.folder_path(entry.parent_id)
        return " / ".join(["Home", *(f.name for f in chain)])

    # -- live Drive scan ------------------------------------------------------

    def _render_scan_rows(self) -> None:
        """Render the live combined listing, filtered by the search box."""
        assert self._scan is not None
        query = self._query.lower()
        self._visible_rows = [
            row for row in self._scan.rows if query in row.file.name.lower()
        ]
        for i, row in enumerate(self._visible_rows):
            self._add_entry(
                self._scan_label(row),
                row.file.name,
                _DRIVE,
                i,
                is_folder=False,
                drive_id=row.file.id,
            )

        if not self._scan.rows:
            self.empty_hint.setText("No files found in any linked account.")
        elif not self._visible_rows:
            self.empty_hint.setText(f"No files match “{self._query}”.")
        self.empty_hint.setVisible(not self._visible_rows)
        self._maybe_fetch_thumbnails()

    def _maybe_fetch_thumbnails(self) -> None:
        """In Gallery mode, pull Drive previews for visible, un-cached rows."""
        if self._view_mode != _VIEW_GALLERY or self._thumb_worker is not None:
            return
        jobs = [
            (row.account_id, row.file.id, row.file.thumbnail_link)
            for row in self._visible_rows
            if row.file.thumbnail_link and row.file.id not in self._thumb_cache
        ]
        if not jobs:
            return
        worker = ThumbnailWorker(jobs, self)
        worker.ready.connect(self._on_thumbnail_ready)
        worker.finished.connect(self._on_thumbnail_worker_finished)
        self._thumb_worker = worker
        worker.start()

    def _on_thumbnail_ready(self, drive_id: str, data: bytes) -> None:
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return
        self._thumb_cache[drive_id] = pixmap
        if self._view_mode != _VIEW_GALLERY:
            return
        icon = QIcon(pixmap)
        for i in range(self.entries.count()):
            item = self.entries.item(i)
            if item.data(_ROLE_DRIVE_ID) == drive_id:
                item.setIcon(icon)

    def _on_thumbnail_worker_finished(self) -> None:
        self._thumb_worker = None
        # Rows that became visible while the worker ran (e.g. filter changed)
        # still need thumbnails — kick off another pass for any left uncached.
        if self._scanning:
            self._maybe_fetch_thumbnails()

    def prepare_to_close(self) -> bool:
        """Stop background threads before this widget is destroyed.

        Qt only delivers QCloseEvent to top-level windows, and this is a tab —
        so its own closeEvent never fires on quit. MainWindow calls this
        instead. It matters: a QThread destroyed while still running aborts the
        whole process (``QThread: Destroyed while thread is still running``),
        which is what a mid-upload quit used to do.

        Returns False if the user chose not to quit after all.
        """
        upload = self._upload_worker
        if upload is not None and upload.isRunning():
            answer = QMessageBox.question(
                self,
                "Upload in progress",
                "An upload is still running.\n\n"
                "Quitting pauses it — nothing already uploaded is lost, and "
                "choosing the same file again will carry on from here.\n\n"
                "Pause the upload and quit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return False
            upload.requestInterruption()
            if not upload.wait(15000):
                # Stuck in a socket read the timeout hasn't tripped yet. Killing
                # the thread is ugly, but far better than aborting the process,
                # and the staged chunks are already committed to the database.
                upload.terminate()
                upload.wait(2000)

        # Auto-starts on every Gallery render, so it may be mid-fetch at quit.
        thumbs = self._thumb_worker
        if thumbs is not None and thumbs.isRunning():
            thumbs.requestInterruption()
            thumbs.wait(3000)
        return True

    def closeEvent(self, event) -> None:
        # Only reached if this widget is ever shown as its own window; the
        # normal quit path is MainWindow -> prepare_to_close().
        if not self.prepare_to_close():
            event.ignore()
            return
        super().closeEvent(event)

    def _scan_label(self, row: ScanRow) -> str:
        f = row.file
        size = human_bytes(f.size) if f.size is not None else "—"
        icon = "📄"
        suffix = ""
        if f.is_google_doc:
            icon = "📝"
            suffix = " · Google Doc (open in browser to export)"
        return f"{icon}  {f.name}    {size} · {row.account_email}{suffix}"

    def _scan_summary_label(self) -> QLabel:
        assert self._scan is not None
        parts = [f"Live Drive scan — {len(self._scan.rows)} files across "
                 f"{self._scanned_account_count()} account(s)"]
        if self._scan.limited_accounts:
            # Re-linking only widens an account if whole-Drive access is
            # actually enabled; otherwise sign-in deliberately asks for the
            # narrow scope and re-linking would change nothing.
            remedy = (
                " until re-linked for full access"
                if config.full_drive_requested()
                else " — whole-Drive access is off, so Gstore sees only its own"
                     " files here"
            )
            parts.append(
                "· " + ", ".join(self._scan.limited_accounts) + " show only "
                "Gstore files" + remedy
            )
        label = QLabel("  ".join(parts))
        label.setWordWrap(True)
        label.setStyleSheet("color: #888;")
        return label

    def _scanned_account_count(self) -> int:
        assert self._scan is not None
        return len({row.account_email for row in self._scan.rows})

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

    def _add_entry(
        self,
        list_text: str,
        name: str,
        kind: str,
        item_id: int,
        *,
        is_folder: bool,
        drive_id: str | None = None,
    ) -> None:
        """Add one row/tile. List mode shows the detailed `list_text`; the icon
        modes show the plain `name` under a real icon (or a Drive thumbnail)."""
        item = QListWidgetItem()
        if self._icon_view:
            item.setText(name)
            item.setIcon(self._icon_for(is_folder, drive_id))
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        else:
            item.setText(list_text)
        item.setData(Qt.ItemDataRole.UserRole, (kind, item_id))
        if drive_id is not None:
            item.setData(_ROLE_DRIVE_ID, drive_id)
        self.entries.addItem(item)

    def _icon_for(self, is_folder: bool, drive_id: str | None) -> QIcon:
        if (
            self._view_mode == _VIEW_GALLERY
            and drive_id is not None
            and drive_id in self._thumb_cache
        ):
            return QIcon(self._thumb_cache[drive_id])
        return self._folder_icon if is_folder else self._file_icon

    def _on_double_click(self, item: QListWidgetItem) -> None:
        kind, item_id = item.data(Qt.ItemDataRole.UserRole)
        if kind == _FOLDER:
            self.navigate_to(item_id)
        elif kind == _DRIVE:
            self._on_download()  # double-click a live result → save it
        elif kind == _FILE and self._searching:
            # Reveal the match in its home folder (also exits search mode).
            entry = self.index.get_file(item_id)
            if entry is not None:
                self.navigate_to(entry.parent_id)

    # -- selection / button state --------------------------------------------

    def _selected(self) -> tuple[str, int] | None:
        item = self.entries.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _busy(self) -> bool:
        return any(
            w is not None
            for w in (
                self._upload_worker,
                self._download_worker,
                self._delete_worker,
                self._scan_worker,
                self._drive_download_worker,
                self._drive_trash_worker,
            )
        )

    def _selected_scan_row(self) -> ScanRow | None:
        selection = self._selected()
        if selection is None or selection[0] != _DRIVE:
            return None
        index = selection[1]
        return self._visible_rows[index] if 0 <= index < len(self._visible_rows) else None

    def _update_buttons(self) -> None:
        selection = self._selected()
        busy = self._busy()
        self.scan_button.setText("Show Gstore files" if self._scanning else "Scan Drive now")
        self.scan_button.setEnabled(not busy or self._scanning)

        # Live-scan mode offers Download (regular binary files; Docs are
        # export-only) and Delete (trash), the latter only for accounts that
        # granted the full `drive` scope. Index-shaping actions (upload / new
        # folder / rename) stay disabled — the scan isn't the index.
        if self._scanning:
            assert self._scan is not None
            row = self._selected_scan_row()
            self.download_button.setEnabled(
                row is not None and row.file.is_downloadable and not busy
            )
            deletable = (
                row is not None
                and row.account_id in self._scan.writable_account_ids
            )
            self.delete_button.setEnabled(deletable and not busy)
            for button in (
                self.upload_button,
                self.new_folder_button,
                self.rename_button,
            ):
                button.setEnabled(False)
            return

        is_file = selection is not None and selection[0] == _FILE

        # Downloading needs the bytes; block it for files whose home account is
        # removed or revoked. Rename/delete stay allowed (index-only + best-effort
        # Drive delete), so unavailable files aren't stuck in the browser.
        downloadable = False
        if is_file:
            entry = self.index.get_file(selection[1])
            downloadable = entry is not None and entry.is_available

        # Upload / new-folder target the current folder, which is ambiguous while
        # searching (results span folders); disable them until search is cleared.
        self.upload_button.setEnabled(not busy and not self._searching)
        self.new_folder_button.setEnabled(not busy and not self._searching)
        self.download_button.setEnabled(downloadable and not busy)
        self.rename_button.setEnabled(selection is not None and not busy)
        self.delete_button.setEnabled(selection is not None and not busy)

    def _begin_transfer(
        self, message: str, determinate: bool, total_bytes: int | None = None
    ) -> None:
        # 0-1000, not 0-100: a 20 GB upload advances ~0.24% per chunk,
        # which an integer percent would round away to no movement at all.
        self.transfer_bar.setRange(0, 1000 if determinate else 0)
        self.transfer_bar.setValue(0)
        self.transfer_bar.setVisible(True)
        self._transfer_message = message
        self._transfer_total = total_bytes
        self._rate_samples.clear()
        self.transfer_status.setText(message)
        self._update_buttons()

    def _set_transfer_message(self, message: str) -> None:
        """Change the phase label without losing the percentage beside it."""
        self._transfer_message = message
        self._render_transfer_status(self.transfer_bar.value() / 1000)

    def _render_transfer_status(self, fraction: float) -> None:
        """Write the phase, the percentage and (when known) the byte counts.

        macOS's native Qt style draws a progress bar's fill but never its text,
        whatever isTextVisible() says — so the percentage has to live in this
        label or it is invisible on the platform the app ships for.
        """
        # One decimal below 10%: 200 MB of 20 GB is 0.98%, and an integer
        # percent shows that real progress as a flat "0%".
        pct = fraction * 100
        parts = [self._transfer_message, f"{pct:.1f}%" if pct < 10 else f"{pct:.0f}%"]
        if self._transfer_total:
            done = int(fraction * self._transfer_total)
            parts.append(
                f"· {human_bytes(done)} of {human_bytes(self._transfer_total)}"
            )
            rate, remaining = self._rate_and_eta(fraction)
            if rate is not None:
                parts.append(f"· {human_rate(rate)}")
                parts.append(f"· {human_duration(remaining)} left")
        self.transfer_status.setText(" ".join(parts))

    def _rate_and_eta(self, fraction: float) -> tuple[float | None, float | None]:
        """Current speed and time remaining, or (None, None) while unknown.

        Measured over a trailing window rather than the whole run, so the
        figure tracks what the connection is doing now instead of being
        dragged around by an early slow or fast patch.

        Only progress made *within* the window counts, which is what keeps a
        resume honest: picking up at 43% must not be read as having just sent
        those 8.7 GB. (It was, once — the worker emitted 0.0 before the first
        real sample, and the estimate came out at 68 MB/s with three minutes
        left on an eleven-gigabyte remainder.)
        """
        total = self._transfer_total
        if not total:
            return None, None

        now = time.monotonic()
        self._rate_samples.append((now, fraction))
        # Keep the window, but never discard so much that fewer than two
        # samples remain to measure between.
        while len(self._rate_samples) > 2 and now - self._rate_samples[0][0] > _RATE_WINDOW_SEC:
            self._rate_samples.popleft()

        start_time, start_fraction = self._rate_samples[0]
        elapsed = now - start_time
        sent = (fraction - start_fraction) * total
        # Too little to be meaningful; an estimate off one sample is noise.
        if elapsed < 3 or sent < 512 * 1024:
            return None, None
        rate = sent / elapsed
        if rate <= 0:
            return None, None
        return rate, (1.0 - fraction) * total / rate

    def _on_transfer_progress(self, fraction: float) -> None:
        self.transfer_bar.setValue(int(fraction * 1000))
        self._render_transfer_status(fraction)

    # -- upload ---------------------------------------------------------------

    def _on_upload(self) -> None:
        if self._busy():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file to upload")
        if not path:
            return
        try:
            total = Path(path).stat().st_size
        except OSError:
            total = None
        self._begin_transfer("Uploading…", determinate=True, total_bytes=total)

        worker = UploadWorker(path, self.current_parent_id, self)
        worker.progress.connect(self._on_transfer_progress)
        worker.succeeded.connect(self._on_upload_succeeded)
        worker.failed.connect(self._on_upload_failed)
        worker.cancelled.connect(self._on_upload_cancelled)
        worker.status.connect(self._set_transfer_message)
        worker.finished.connect(self._on_upload_finished)
        self._upload_worker = worker
        self.cancel_button.setEnabled(True)
        self.cancel_button.setVisible(True)
        worker.start()

    def _on_cancel_upload(self) -> None:
        worker = self._upload_worker
        if worker is None or not worker.isRunning():
            return
        # The worker checks between chunks and during retry backoff, so this is
        # not instant; disable the button so it cannot be pressed twice.
        self.cancel_button.setEnabled(False)
        self.transfer_status.setText("Stopping after the current chunk…")
        worker.requestInterruption()

    def _on_upload_cancelled(self) -> None:
        self.transfer_status.setText(
            "Upload paused — choose the same file again to carry on."
        )
        self.refresh()

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
        self.cancel_button.setVisible(False)
        self._update_buttons()

    # -- live Drive scan ------------------------------------------------------

    def _on_scan_toggle(self) -> None:
        if self._scanning:
            self._exit_scan()
            return
        if self._busy():
            return
        worker = DriveScanWorker(self)
        worker.finished_scan.connect(self._on_scan_finished)
        worker.finished.connect(self._on_scan_worker_finished)
        self._scan_worker = worker  # set before _begin_transfer so buttons disable
        self._begin_transfer("Scanning all accounts…", determinate=False)
        worker.start()

    def _on_scan_finished(self, result: ScanResult) -> None:
        self._scan = result
        # Search box now filters the live results instead of the index.
        self.search_box.setPlaceholderText("Filter scanned files…")
        if result.failures:
            summary = "; ".join(f"{email}: {msg}" for email, msg in result.failures)
            self.transfer_status.setText(f"Some accounts could not be scanned — {summary}")
        else:
            self.transfer_status.setText(f"Scanned {len(result.rows)} files.")
        self.refresh()

    def _on_scan_worker_finished(self) -> None:
        self._scan_worker = None
        self.transfer_bar.setVisible(False)
        self._update_buttons()

    def _exit_scan(self) -> None:
        self._scan = None
        self._visible_rows = []
        self.search_box.setPlaceholderText("Search all files…")
        self.transfer_status.setText("")
        self.refresh()

    # -- download -------------------------------------------------------------

    def _on_download(self) -> None:
        if self._busy():
            return
        if self._scanning:
            self._download_scan_row()
            return
        selection = self._selected()
        if selection is None or selection[0] != _FILE:
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

    def _download_scan_row(self) -> None:
        row = self._selected_scan_row()
        if row is None or not row.file.is_downloadable:
            return
        dest, _ = QFileDialog.getSaveFileName(self, "Save file as", row.file.name)
        if not dest:
            return
        self._begin_transfer("Downloading…", determinate=True)

        worker = DriveDownloadWorker(row.account_id, row.file.id, dest, row.file.md5, self)
        worker.progress.connect(self._on_transfer_progress)
        worker.succeeded.connect(self._on_download_succeeded)
        worker.failed.connect(self._on_download_failed)
        worker.finished.connect(self._on_drive_download_finished)
        self._drive_download_worker = worker
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

    def _on_drive_download_finished(self) -> None:
        self._drive_download_worker = None
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
        if self._busy():
            return
        if self._scanning:
            self._trash_scan_row()
            return
        selection = self._selected()
        if selection is None:
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

    # -- trash a scanned file -------------------------------------------------

    def _trash_scan_row(self) -> None:
        row = self._selected_scan_row()
        assert self._scan is not None
        if row is None or row.account_id not in self._scan.writable_account_ids:
            return
        message = (
            f"Move “{row.file.name}” to Trash on {row.account_email}?\n\n"
            "It leaves your Drive but stays recoverable from Google Drive's "
            "Trash for about 30 days."
        )
        if (
            QMessageBox.question(self, "Move to Trash", message)
            != QMessageBox.StandardButton.Yes
        ):
            return

        self._trashing = row
        self._begin_transfer("Moving to Trash…", determinate=False)
        worker = DriveTrashWorker(row.account_id, row.file.id, self)
        worker.succeeded.connect(self._on_trash_succeeded)
        worker.failed.connect(self._on_trash_failed)
        worker.finished.connect(self._on_trash_finished)
        self._drive_trash_worker = worker
        worker.start()

    def _on_trash_succeeded(self) -> None:
        # Drop the trashed file from the in-memory scan so it disappears without
        # a full re-scan; a fresh scan would no longer list it anyway.
        row = self._trashing
        if row is not None and self._scan is not None:
            self._scan.rows = [r for r in self._scan.rows if r is not row]
        self.transfer_status.setText("Moved to Trash.")
        self.refresh()

    def _on_trash_failed(self, message: str) -> None:
        self.transfer_status.setText("")
        QMessageBox.warning(self, "Delete failed", message)

    def _on_trash_finished(self) -> None:
        self._drive_trash_worker = None
        self._trashing = None
        self.transfer_bar.setVisible(False)
        self._update_buttons()
