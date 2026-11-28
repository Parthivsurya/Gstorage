"""Tests for the update download's progress window.

A percentage alone cannot tell someone whether a slow download is working or
stuck. These check the figures that can: how much has arrived, how fast, and
how much longer — and that a download with no declared size degrades to
something honest rather than a made-up position.
"""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from gstore.ui.update_progress_dialog import UpdateProgressDialog

MB = 1024 * 1024


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(app):
    return UpdateProgressDialog("9.9.9")


def test_it_names_the_version_being_fetched(dialog):
    assert "9.9.9" in dialog.windowTitle()
    assert "9.9.9" in dialog.heading.text()


def test_the_bar_tracks_the_fraction_downloaded(dialog):
    dialog.on_progress(38 * MB, 76 * MB)

    assert dialog.bar.maximum() == 1000
    assert dialog.bar.value() == pytest.approx(500, abs=2)


def test_it_shows_how_much_of_how_much(dialog):
    dialog.on_progress(45 * MB, 76 * MB)

    text = dialog.detail.text()
    assert "45" in text and "76" in text, text
    assert " of " in text


def test_a_speed_appears_once_there_is_something_to_measure(dialog, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(
        "gstore.ui.update_progress_dialog.time.monotonic", lambda: clock["t"]
    )

    dialog.on_progress(0, 10 * MB)
    clock["t"] += 1.0
    dialog.on_progress(2 * MB, 10 * MB)

    assert dialog._rate() == pytest.approx(2 * MB, rel=0.01)
    assert "/s" in dialog.detail.text()


def test_time_remaining_is_shown_while_there_is_some(dialog, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "gstore.ui.update_progress_dialog.time.monotonic", lambda: clock["t"]
    )
    dialog.on_progress(0, 10 * MB)
    clock["t"] += 1.0
    dialog.on_progress(1 * MB, 10 * MB)      # 1 MB/s, 9 MB to go

    assert "left" in dialog.detail.text(), dialog.detail.text()


def test_no_time_remaining_once_it_is_finished(dialog, monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "gstore.ui.update_progress_dialog.time.monotonic", lambda: clock["t"]
    )
    dialog.on_progress(0, 10 * MB)
    clock["t"] += 1.0
    dialog.on_progress(10 * MB, 10 * MB)

    assert "left" not in dialog.detail.text()


def test_one_sample_yields_no_speed(dialog):
    """Two points are needed; inventing one from a single reading is a lie."""
    dialog.on_progress(1 * MB, 10 * MB)

    assert dialog._rate() is None


def test_the_speed_follows_a_trailing_window(dialog, monkeypatch):
    """A fast first second must not flatter the figure a minute later."""
    clock = {"t": 0.0}
    monkeypatch.setattr(
        "gstore.ui.update_progress_dialog.time.monotonic", lambda: clock["t"]
    )
    dialog.on_progress(0, 100 * MB)
    clock["t"] += 1.0
    dialog.on_progress(50 * MB, 100 * MB)        # a very fast burst
    for _ in range(8):                            # then a slow minute
        clock["t"] += 1.0
        dialog.on_progress(dialog._samples[-1][1] + 1 * MB, 100 * MB)

    assert dialog._rate() == pytest.approx(1 * MB, rel=0.2), (
        "the early burst is still being counted"
    )


def test_an_unknown_total_shows_movement_not_a_false_position(dialog):
    """No Content-Length: a busy bar is honest, a percentage would not be."""
    dialog.on_progress(5 * MB, 0)

    assert dialog.bar.maximum() == 0, "should be indeterminate"
    assert "of" not in dialog.detail.text()


def test_cancelling_is_recorded_and_says_so(dialog):
    assert dialog.cancelled is False

    dialog.cancel.click()

    assert dialog.cancelled is True
    assert dialog.cancel.isEnabled() is False
    assert "Stopping" in dialog.detail.text()


def test_the_verifying_step_is_visible(dialog):
    """Checking a 76 MB signature is not instant; silence looks like a hang."""
    dialog.on_progress(76 * MB, 76 * MB)
    dialog.on_verifying()

    assert "signature" in dialog.heading.text().lower()
    assert dialog.bar.maximum() == 0
    assert dialog.cancel.isEnabled() is False
