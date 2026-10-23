"""Tests for the chunked-UPLOAD half (placement, staging, resume, cancel).

The download half has had tests since it landed; this side had none, which is
why a resume reported the whole file's size as every chunk's size for as long
as it did. These cover the paths a flaky network actually takes: an upload cut
off partway, resumed, cancelled, retried, or reading a file that changed under
it.

The network is stubbed throughout — `_upload_bytes` is replaced with a fake
backed by an in-memory {drive_file_id: bytes} store that mirrors the real
function's contract (returns id + md5, raises on failure).
"""

from __future__ import annotations

import hashlib
import socket
import ssl

import pytest

import gstore.drive.chunk_upload as chunk_upload
from gstore.auth.accounts import Account
from gstore.db.database import Database
from gstore.drive.chunk_upload import ChunkUploadResult, upload_chunks
from gstore.drive.hashing import IntegrityError
from gstore.drive.placement import (
    chunk_ranges,
    choose_account,
    plan_chunk_placement,
    plan_missing_chunks,
)
from gstore.drive.rate_limit import Cancelled, execute_with_backoff
from gstore.index.pending_chunks import PendingChunks

MB = 1024 * 1024


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _account(account_id: int, free: int | None, *, synced: bool = True) -> Account:
    """An Account with `free` bytes available (None == unlimited)."""
    return Account(
        id=account_id,
        email=f"acct{account_id}@example.com",
        quota_limit_bytes=None if free is None else 100 * MB,
        quota_used_bytes=None if free is None else 100 * MB - free,
        quota_synced_at="2026-01-01T00:00:00" if synced else None,
        status="active",
        created_at="2026-01-01T00:00:00",
    )


class FakeDrive:
    """In-memory stand-in for the per-chunk Drive upload."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.uploads: list[tuple[str, int]] = []  # (name, byte count), in order
        self.deleted: list[str] = []
        self.fail_on: int | None = None
        self.raise_with: Exception | None = None
        self._n = 0

    def upload_bytes(self, credentials, name, data, should_stop=None,
                     on_progress=None):
        self._n += 1
        if self.fail_on is not None and self._n == self.fail_on:
            raise self.raise_with or ConnectionResetError(54, "Connection reset")
        # Mirror the real function reporting its inner resumable pieces.
        if on_progress is not None:
            for frac in (0.5, 1.0):
                on_progress(frac)
        drive_id = f"drive-{name}"
        self.store[drive_id] = data
        self.uploads.append((name, len(data)))
        return drive_id, _md5(data)

    def delete(self, credentials, drive_id):
        self.deleted.append(drive_id)
        self.store.pop(drive_id, None)


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(chunk_upload, "_upload_bytes", fake.upload_bytes)
    monkeypatch.setattr(chunk_upload, "delete_drive_file", fake.delete)
    return fake


class Resume:
    """Minimal in-memory ChunkResume, like the worker's _ChunkResume."""

    def __init__(self):
        self.rows: dict[int, ChunkUploadResult] = {}

    def completed(self):
        return dict(self.rows)

    def record(self, result):
        self.rows[result.sequence] = result


def _creds_for(account_id: int) -> object:
    return object()


def _write(tmp_path, size: int) -> tuple[object, bytes]:
    # A repeating byte pattern: distinguishes chunk boundaries without the
    # cost of generating megabytes one byte at a time.
    data = (bytes(range(256)) * (size // 256 + 1))[:size]
    path = tmp_path / "big.bin"
    path.write_bytes(data)
    return path, data


# -- happy path --------------------------------------------------------------


def test_uploads_every_chunk_in_sequence_order(drive, tmp_path):
    path, data = _write(tmp_path, 5 * MB)
    plans = plan_chunk_placement([_account(1, 50 * MB)], len(data), chunk_size=MB)

    results = upload_chunks(path, plans, _creds_for)

    assert [r.sequence for r in results] == [0, 1, 2, 3, 4]
    assert b"".join(drive.store[r.drive_file_id] for r in results) == data


def test_each_chunk_records_its_own_length_not_the_file_size(drive, tmp_path):
    path, data = _write(tmp_path, 5 * MB)
    plans = plan_chunk_placement([_account(1, 50 * MB)], len(data), chunk_size=2 * MB)

    results = upload_chunks(path, plans, _creds_for)

    assert [r.size_bytes for r in results] == [2 * MB, 2 * MB, MB]
    assert sum(r.size_bytes for r in results) == len(data)


def test_progress_moves_inside_a_chunk_not_only_between_them(drive, tmp_path):
    """A 20 GB file is 410 chunks; chunk-granular progress looks frozen."""
    path, data = _write(tmp_path, 4 * MB)
    plans = plan_chunk_placement([_account(1, 50 * MB)], len(data), chunk_size=MB)

    seen: list[float] = []
    upload_chunks(path, plans, _creds_for, on_progress=seen.append)

    # 4 chunks, each reporting twice -> more updates than there are chunks
    assert len(seen) > len(plans)
    assert seen == sorted(seen)
    assert seen[-1] == pytest.approx(1.0)


def test_progress_is_monotonic_and_reaches_one(drive, tmp_path):
    path, data = _write(tmp_path, 4 * MB)
    plans = plan_chunk_placement([_account(1, 50 * MB)], len(data), chunk_size=MB)

    seen: list[float] = []
    upload_chunks(path, plans, _creds_for, on_progress=seen.append)

    assert seen == sorted(seen)
    assert seen[-1] == pytest.approx(1.0)


# -- interruption and resume -------------------------------------------------


def test_interrupted_upload_keeps_staged_chunks(drive, tmp_path):
    """The whole point of staging: a dropped link must not lose finished work."""
    path, data = _write(tmp_path, 5 * MB)
    plans = plan_chunk_placement([_account(1, 50 * MB)], len(data), chunk_size=MB)
    resume = Resume()
    drive.fail_on = 4  # network dies partway through

    with pytest.raises(ConnectionResetError):
        upload_chunks(path, plans, _creds_for, resume=resume)

    assert sorted(resume.rows) == [0, 1, 2]
    assert drive.deleted == []  # staged, not cleaned up


def test_resume_uploads_only_the_missing_chunks(drive, tmp_path):
    path, data = _write(tmp_path, 5 * MB)
    accounts = [_account(1, 50 * MB)]
    resume = Resume()
    drive.fail_on = 4

    plans = plan_chunk_placement(accounts, len(data), chunk_size=MB)
    with pytest.raises(ConnectionResetError):
        upload_chunks(path, plans, _creds_for, resume=resume)

    drive.fail_on = None
    before = len(drive.uploads)
    missing = plan_missing_chunks(accounts, len(data), set(resume.rows), MB)
    results = upload_chunks(path, missing, _creds_for, resume=resume)

    assert len(drive.uploads) - before == 2  # only chunks 3 and 4 re-sent
    assert [r.sequence for r in results] == [0, 1, 2, 3, 4]
    assert b"".join(drive.store[r.drive_file_id] for r in results) == data


def test_resumed_chunks_keep_their_real_sizes(drive, tmp_path):
    """Regression: a resume once reported the file size as every chunk's size."""
    path, data = _write(tmp_path, 5 * MB)
    accounts = [_account(1, 50 * MB)]
    resume = Resume()
    drive.fail_on = 3

    with pytest.raises(ConnectionResetError):
        upload_chunks(
            path,
            plan_chunk_placement(accounts, len(data), chunk_size=2 * MB),
            _creds_for,
            resume=resume,
        )
    drive.fail_on = None
    missing = plan_missing_chunks(accounts, len(data), set(resume.rows), 2 * MB)
    results = upload_chunks(path, missing, _creds_for, resume=resume)

    assert [r.size_bytes for r in results] == [2 * MB, 2 * MB, MB]
    assert all(r.size_bytes <= 2 * MB for r in results)


def test_without_a_resume_store_failed_upload_cleans_up(drive, tmp_path):
    path, data = _write(tmp_path, 5 * MB)
    plans = plan_chunk_placement([_account(1, 50 * MB)], len(data), chunk_size=MB)
    drive.fail_on = 4

    with pytest.raises(ConnectionResetError):
        upload_chunks(path, plans, _creds_for)  # no resume=

    assert len(drive.deleted) == 3  # no orphans left behind


# -- cancellation ------------------------------------------------------------


def test_cancel_stops_between_chunks_and_keeps_progress(drive, tmp_path):
    path, data = _write(tmp_path, 5 * MB)
    plans = plan_chunk_placement([_account(1, 50 * MB)], len(data), chunk_size=MB)
    resume = Resume()

    with pytest.raises(Cancelled):
        upload_chunks(
            path,
            plans,
            _creds_for,
            resume=resume,
            should_stop=lambda: len(resume.rows) >= 2,
        )

    assert sorted(resume.rows) == [0, 1]
    assert drive.deleted == []  # cancelling is a pause, not a discard


# -- the file changing underneath us -----------------------------------------


def test_truncated_file_is_refused_not_silently_short_uploaded(drive, tmp_path):
    path, data = _write(tmp_path, 5 * MB)
    plans = plan_chunk_placement([_account(1, 50 * MB)], len(data), chunk_size=MB)
    path.write_bytes(data[: 2 * MB])  # shrinks mid-upload

    with pytest.raises(IntegrityError, match="changed while uploading"):
        upload_chunks(path, plans, _creds_for)

    assert all(n == MB for _name, n in drive.uploads)  # no short chunk stored


# -- placement ---------------------------------------------------------------


def test_chunk_ranges_tile_the_file_exactly():
    ranges = chunk_ranges(5 * MB + 7, chunk_size=2 * MB)
    assert sum(length for _s, _o, length in ranges) == 5 * MB + 7
    offsets = [o for _s, o, _l in ranges]
    assert offsets == sorted(offsets)
    for (_s, off, length), (_s2, nxt, _l2) in zip(ranges, ranges[1:]):
        assert off + length == nxt  # contiguous, no gaps or overlaps


def test_chunks_spread_across_accounts_by_free_space():
    accounts = [_account(1, 3 * MB), _account(2, 3 * MB)]
    plans = plan_chunk_placement(accounts, 4 * MB, chunk_size=MB)
    assert plans is not None
    assert {p.account_id for p in plans} == {1, 2}


def test_placement_needs_one_account_to_fit_a_whole_chunk():
    """Documents the per-account floor: pooled headroom is not enough."""
    many_small = [_account(i, MB // 2) for i in range(1, 11)]  # 5 MB pooled
    assert plan_chunk_placement(many_small, 4 * MB, chunk_size=MB) is None

    one_big = many_small + [_account(99, 4 * MB)]
    assert plan_chunk_placement(one_big, 4 * MB, chunk_size=MB) is not None


def test_unsynced_accounts_are_not_used_for_placement():
    assert choose_account([_account(1, 50 * MB, synced=False)], MB) is None
    assert plan_chunk_placement([_account(1, 50 * MB, synced=False)], MB, MB) is None


def test_plan_missing_chunks_returns_empty_when_all_done():
    done = {seq for seq, _o, _l in chunk_ranges(3 * MB, MB)}
    assert plan_missing_chunks([_account(1, 50 * MB)], 3 * MB, done, MB) == []


# -- the pending-chunk store -------------------------------------------------


@pytest.fixture
def pending(tmp_path):
    db = Database(tmp_path / "idx.db").open().initialize()
    yield PendingChunks(db)
    db.close()


def test_completed_returns_chunk_length_not_file_size(pending):
    """Regression for the bug this column was added to prevent."""
    file_size = 120 * MB
    pending.record("/f.bin", file_size, 111, 0, 1, "d0", "m0", chunk_size=50 * MB)

    (row,) = pending.completed("/f.bin", file_size, 111)

    assert row["chunk_size"] == 50 * MB
    assert row["chunk_size"] != file_size


def test_staged_chunks_are_scoped_to_one_exact_file(pending):
    pending.record("/f.bin", 100, 111, 0, 1, "d0", "m0", chunk_size=50)
    assert pending.completed("/f.bin", 100, 111)
    assert pending.completed("/f.bin", 100, 222) == []  # edited: different mtime
    assert pending.completed("/f.bin", 999, 111) == []  # different size


def test_reset_if_changed_drops_rows_for_an_edited_file(pending):
    pending.record("/f.bin", 100, 111, 0, 1, "d0", "m0", chunk_size=50)
    pending.reset_if_changed("/f.bin", 100, 222)
    assert pending.completed("/f.bin", 100, 111) == []


def test_file_md5_is_cached_and_scoped(pending):
    assert pending.file_md5("/f.bin", 100, 111) is None
    pending.record("/f.bin", 100, 111, 0, 1, "d0", "m0", 50, file_md5="abc123")
    assert pending.file_md5("/f.bin", 100, 111) == "abc123"
    assert pending.file_md5("/f.bin", 100, 222) is None


def test_clear_removes_every_staged_chunk(pending):
    for seq in range(3):
        pending.record("/f.bin", 100, 111, seq, 1, f"d{seq}", "m", chunk_size=50)
    pending.clear("/f.bin")
    assert pending.completed("/f.bin", 100, 111) == []


# -- retry policy ------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        socket.timeout("timed out"),
        ConnectionResetError(54, "Connection reset by peer"),
        socket.gaierror(8, "nodename nor servname provided"),
        ssl.SSLEOFError("EOF occurred in violation of protocol"),
    ],
)
def test_transport_errors_are_retried(error):
    """A dropped link is the usual way a long upload dies; it must recover."""
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise error
        return "ok"

    assert execute_with_backoff(call, base_delay=0.001) == "ok"
    assert attempts["n"] == 3


def test_certificate_errors_are_not_retried():
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        raise ssl.SSLCertVerificationError("bad certificate")

    with pytest.raises(ssl.SSLError):
        execute_with_backoff(call, base_delay=0.001)
    assert attempts["n"] == 1  # retrying would only delay the inevitable


def test_retries_are_bounded():
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        raise ConnectionResetError(54, "Connection reset by peer")

    with pytest.raises(ConnectionResetError):
        execute_with_backoff(call, max_retries=3, base_delay=0.001)
    assert attempts["n"] == 4  # the first try plus three retries


def test_cancel_is_noticed_without_waiting_out_the_backoff():
    import time

    stop = {"v": False}

    def call():
        stop["v"] = True  # cancel arrives while the next backoff is pending
        raise ConnectionResetError(54, "Connection reset by peer")

    started = time.monotonic()
    with pytest.raises(Cancelled):
        execute_with_backoff(call, base_delay=30.0, should_stop=lambda: stop["v"])
    assert time.monotonic() - started < 2.0
