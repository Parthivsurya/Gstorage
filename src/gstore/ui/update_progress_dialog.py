"""A progress window for an update download.

The banner reported progress as a bare percentage in its own label, which
says nothing about whether a stalled-looking download is slow or stuck. A
release asset is 70-100 MB and the connection it arrives over is often the
reason it takes a while, so the figures that answer "is this working?" are
how much has arrived, how fast, and how much longer.

Modal on purpose. Starting a second download, or quitting mid-transfer,
while one is running has no good outcome, and a window that must be dealt
with is simpler than disabling buttons across two tabs.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from gstore.format import human_bytes, human_duration, human_rate

# Speed over a trailing window rather than the whole download: the figure
# should describe what the connection is doing now, not be dragged around by
# a fast first second or a slow patch three minutes ago.
_WINDOW_SEC = 5.0


class UpdateProgressDialog(QDialog):
    """Shows bytes, speed and time remaining while an update downloads."""

    def __init__(self, version: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Downloading Gstore {version}")
        self.setModal(True)
        self.setMinimumWidth(420)
        self._samples: list[tuple[float, int]] = []
        self._cancelled = False

        self.heading = QLabel(f"Downloading Gstore {version}…")
        self.heading.setStyleSheet("font-weight: 600;")

        # 0-1000, not 0-100: the same reason the transfer bar uses it. On a
        # slow line an integer percent sits still for seconds at a time.
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)

        # macOS never draws a progress bar's text whatever Qt is told, so the
        # numbers live in a label or they are invisible on the platform this
        # ships for.
        self.detail = QLabel("Starting…")
        self.detail.setStyleSheet("color: #666;")
        self.detail.setTextFormat(Qt.TextFormat.PlainText)

        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self._on_cancel)
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.cancel)

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(self.heading)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail)
        layout.addLayout(buttons)
        self.setLayout(layout)

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def _on_cancel(self) -> None:
        self._cancelled = True
        self.cancel.setEnabled(False)
        self.detail.setText("Stopping…")

    def on_progress(self, read: int, total: int) -> None:
        """Update the bar and the figures beside it."""
        now = time.monotonic()
        self._samples.append((now, read))
        while self._samples and now - self._samples[0][0] > _WINDOW_SEC:
            self._samples.pop(0)

        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(min(read / total, 1.0) * 1000))
        else:
            # No Content-Length: show movement rather than a false position.
            self.bar.setRange(0, 0)

        parts = [
            f"{human_bytes(read)} of {human_bytes(total)}"
            if total
            else human_bytes(read)
        ]
        rate = self._rate()
        if rate:
            parts.append(human_rate(rate))
            if total and read < total:
                parts.append(f"{human_duration((total - read) / rate)} left")
        self.detail.setText("  ·  ".join(parts))

    def _rate(self) -> float | None:
        """Bytes per second over the trailing window, or None while unknown."""
        if len(self._samples) < 2:
            return None
        (first_at, first_read), (last_at, last_read) = (
            self._samples[0],
            self._samples[-1],
        )
        elapsed = last_at - first_at
        if elapsed <= 0:
            return None
        return (last_read - first_read) / elapsed

    def on_verifying(self) -> None:
        """The bytes are in; the signature is being checked."""
        self.bar.setRange(0, 0)
        self.heading.setText("Checking the signature…")
        self.detail.setText(
            "Making sure this really is a Gstore release before it is used."
        )
        self.cancel.setEnabled(False)
