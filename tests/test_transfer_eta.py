"""Tests for the transfer speed/ETA readout (gstore.ui.file_browser).

An ETA is only useful if it is honest, and this one shipped dishonest: the
worker emitted a 0.0 progress sample to reset the bar after checksumming, the
UI took that as the start of the run, and a resume at 43% was therefore
credited with having just sent 8.8 GB. It read "68.0 MB/s · 2m 47s left" with
eleven gigabytes still to go.

So these pin the two properties that matter: progress carried over from a
previous run is never counted as this run's throughput, and the figure tracks
the current rate rather than an average over everything.
"""

from __future__ import annotations

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gstore.db.database import Database  # noqa: E402
from gstore.format import human_duration, human_rate  # noqa: E402
from gstore.index import IndexStore  # noqa: E402
from gstore.ui.file_browser import FileBrowser  # noqa: E402

GB = 1024**3
MB = 1024**2


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def browser(qapp, tmp_path):
    db = Database(tmp_path / "idx.db").open().initialize()
    yield FileBrowser(IndexStore(db))
    db.close()


def _feed(browser, samples, total):
    """Replay (seconds_ago, fraction) samples as if they had arrived live."""
    now = time.monotonic()
    browser._rate_samples.clear()
    for ago, frac in samples[:-1]:
        browser._rate_samples.append((now - ago, frac))
    return browser._rate_and_eta(samples[-1][1])


# -- the regression ----------------------------------------------------------


def test_resumed_progress_is_not_counted_as_this_run(browser):
    """The bug: a resume at 43% reported 68 MB/s and minutes to go."""
    browser._begin_transfer("Uploading…", determinate=True, total_bytes=20 * GB)

    browser._on_transfer_progress(0.435)  # handover at the resumed fraction
    rate, eta = browser._rate_and_eta(0.435)

    assert rate is None  # nothing has actually been sent yet
    assert eta is None
    assert "MB/s" not in browser.transfer_status.text()


def test_rate_reflects_only_bytes_moved_in_the_window(browser):
    """60 s of genuine 3 MB/s must read as 3 MB/s, not as the resumed bulk."""
    total = 20 * GB
    browser._begin_transfer("Uploading…", determinate=True, total_bytes=total)
    start = 0.435
    moved = 3 * MB * 60

    rate, eta = _feed(
        browser,
        [(60.0, start), (0.0, start + moved / total)],
        total,
    )

    assert rate == pytest.approx(3 * MB, rel=0.05)
    remaining = (1 - (start + moved / total)) * total
    assert eta == pytest.approx(remaining / (3 * MB), rel=0.05)


def test_estimate_tracks_a_slowdown_rather_than_averaging_it_away(browser):
    """A trailing window is the point: old fast samples must age out."""
    total = 10 * GB
    browser._begin_transfer("Uploading…", determinate=True, total_bytes=total)
    # 10 MB/s long ago, 1 MB/s recently; only the recent part is in the window
    now_frac = 0.5
    rate, _eta = _feed(
        browser,
        [
            (600.0, 0.0),  # far outside the window, must be dropped
            (20.0, now_frac - (1 * MB * 20) / total),
            (0.0, now_frac),
        ],
        total,
    )
    assert rate == pytest.approx(1 * MB, rel=0.2)


def test_no_estimate_until_there_is_something_to_measure(browser):
    total = 20 * GB
    browser._begin_transfer("Uploading…", determinate=True, total_bytes=total)
    # a single sample, and a sliver of a second
    rate, eta = _feed(browser, [(0.5, 0.10), (0.0, 0.1001)], total)
    assert (rate, eta) == (None, None)


def test_unknown_total_yields_no_estimate(browser):
    browser._begin_transfer("Downloading…", determinate=True, total_bytes=None)
    assert browser._rate_and_eta(0.5) == (None, None)


def test_a_new_transfer_does_not_inherit_the_previous_one_s_samples(browser):
    total = 20 * GB
    browser._begin_transfer("Uploading…", determinate=True, total_bytes=total)
    _feed(browser, [(60.0, 0.0), (0.0, 0.9)], total)
    assert browser._rate_samples  # the first transfer left samples behind

    browser._begin_transfer("Uploading…", determinate=True, total_bytes=total)
    assert not browser._rate_samples
    assert browser._rate_and_eta(0.0) == (None, None)


# -- formatting --------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [(0.4, "1s"), (59, "59s"), (60, "1m"), (95, "1m 35s"),
     (3600, "1h"), (4500, "1h 15m"), (86400, "1d"), (None, "—"), (-1, "—")],
)
def test_human_duration(seconds, expected):
    assert human_duration(seconds) == expected


def test_human_duration_handles_nan():
    assert human_duration(float("nan")) == "—"


@pytest.mark.parametrize(
    "rate,expected",
    [(1024, "1.0 KB/s"), (12 * MB, "12.0 MB/s"), (0, "—"), (None, "—")],
)
def test_human_rate(rate, expected):
    assert human_rate(rate) == expected


# -- 64-bit sizes ------------------------------------------------------------


def test_a_twenty_gigabyte_total_survives_the_signal():
    """Regression: Signal(int) is a 32-bit C++ int and truncated the total.

    19.97 GB (21,442,772,305) wrapped to -32,064,175, and the label read
    "-62718 B of -32064175 B".
    """
    from gstore.ui.download_worker import DownloadWorker

    received: list[int] = []
    worker = DownloadWorker(1, "/tmp/unused")
    worker.total_bytes.connect(received.append)
    worker.total_bytes.emit(21_442_772_305)

    assert received == [21_442_772_305]


def test_eta_is_sane_for_a_twenty_gigabyte_download(browser):
    total = 21_442_772_305
    browser._begin_transfer("Downloading…", determinate=True, total_bytes=None)
    browser._set_transfer_total(total)
    browser._on_transfer_progress(0.002)

    text = browser.transfer_status.text()
    assert "20.0 GB" in text
    assert "-" not in text.replace("—", "")  # no negative byte counts


def test_human_bytes_scales_negatives_instead_of_dumping_raw_bytes():
    from gstore.format import human_bytes

    assert human_bytes(-32_064_175) == "-30.6 MB"
    assert human_bytes(21_442_772_305) == "20.0 GB"
