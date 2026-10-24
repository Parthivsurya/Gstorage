"""Tests for parallel chunked download: resume, space, cancel, reporting.

Downloads used to be sequential and threw everything away on failure: one
timed-out chunk — 400 of 409, say — deleted the output and sent a 20 GB
transfer back to zero, reporting only "timed out".

They are now fetched several at a time, which was worth doing — measured
against Drive over 5G, one stream was held to ~1.3 Mbps against an 80 Mb/s
link while eight streams moved eight times the data in the same wall time.
Concurrency means chunks land out of order, so the output is preallocated and
written at offsets, and which sequences actually arrived is recorded in the
database rather than inferred from the file's length.

These pin that: an interrupted download resumes from what landed, out-of-order
writes still reassemble correctly, a download that cannot fit is refused before
anything is fetched, and a failure names the chunk.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import socket
import threading

import pytest

import gstore.drive.chunk_download as chunk_download
from gstore.db.database import Database
from gstore.drive.chunk_download import (
    NotEnoughSpace,
    download_chunks,
    part_path_for,
    stream_count,
)
from gstore.drive.hashing import IntegrityError
from gstore.drive.rate_limit import Cancelled
from gstore.index import Chunk
from gstore.drive.positional_io import pwrite
from gstore.index.download_progress import DownloadProgress

CHUNK = 64 * 1024
FILE_ID = 7


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _chunks(payloads: list[bytes]) -> list[Chunk]:
    return [
        Chunk(
            id=i,
            file_id=FILE_ID,
            sequence=i,
            account_id=100 + (i % 2),
            account_email=f"a{i % 2}@example.com",
            account_status="active",
            drive_file_id=f"drive-{i}",
            md5=_md5(p),
            size_bytes=len(p),
        )
        for i, p in enumerate(payloads)
    ]


@pytest.fixture
def cloud(monkeypatch):
    """Fake _fetch_chunk over an in-memory store, with a failure switch."""
    state = {
        "store": {},
        "fail_seq": None,
        "fetched": [],
        "concurrent": 0,
        "peak": 0,
        "lock": threading.Lock(),
        "delay": 0.0,
    }

    def fake_fetch(credentials, chunk, fd, offset, should_stop=None):
        with state["lock"]:
            state["concurrent"] += 1
            state["peak"] = max(state["peak"], state["concurrent"])
        try:
            if should_stop is not None and should_stop():
                raise Cancelled("download cancelled")
            if state["delay"]:
                import time

                time.sleep(state["delay"])
            if state["fail_seq"] is not None and chunk.sequence == state["fail_seq"]:
                raise socket.timeout("timed out")
            data = state["store"][chunk.drive_file_id]
            pwrite(fd, data, offset)
            with state["lock"]:
                state["fetched"].append(chunk.sequence)
            return len(data)
        finally:
            with state["lock"]:
                state["concurrent"] -= 1

    monkeypatch.setattr(chunk_download, "_fetch_chunk", fake_fetch)
    return state


@pytest.fixture
def progress(tmp_path):
    db = Database(tmp_path / "idx.db").open().initialize()
    yield DownloadProgress(db)
    db.close()


def _creds_for(account_id):
    return object()


def _setup(cloud, count=6):
    payloads = [bytes([i]) * CHUNK for i in range(count)]
    chunks = _chunks(payloads)
    for c, p in zip(chunks, payloads):
        cloud["store"][c.drive_file_id] = p
    return chunks, b"".join(payloads)


def _run(chunks, dest, progress=None, **kw):
    return download_chunks(
        chunks, _creds_for, dest, progress_store=progress, file_id=FILE_ID, **kw
    )


# -- correctness under concurrency -------------------------------------------


def test_reassembles_correctly_despite_out_of_order_completion(tmp_path, cloud):
    chunks, whole = _setup(cloud, count=12)
    dest = tmp_path / "out.bin"
    cloud["delay"] = 0.01  # let the pool genuinely interleave

    _run(chunks, dest, expected_md5=_md5(whole), streams=4)

    assert dest.read_bytes() == whole
    assert cloud["peak"] > 1  # they really did overlap


def test_uses_several_streams(tmp_path, cloud):
    chunks, whole = _setup(cloud, count=12)
    dest = tmp_path / "out.bin"
    cloud["delay"] = 0.02

    _run(chunks, dest, expected_md5=_md5(whole), streams=4)

    assert cloud["peak"] >= 2


def test_single_stream_still_works(tmp_path, cloud):
    chunks, whole = _setup(cloud)
    dest = tmp_path / "out.bin"
    _run(chunks, dest, expected_md5=_md5(whole), streams=1)
    assert dest.read_bytes() == whole
    assert cloud["peak"] == 1


def test_destination_appears_only_on_success(tmp_path, cloud, progress):
    chunks, _ = _setup(cloud)
    dest = tmp_path / "out.bin"
    cloud["fail_seq"] = 2

    with pytest.raises(socket.timeout):
        _run(chunks, dest, progress, streams=1)

    assert not dest.exists()
    assert part_path_for(dest).exists()


# -- resume ------------------------------------------------------------------


def test_resume_fetches_only_what_is_missing(tmp_path, cloud, progress):
    chunks, whole = _setup(cloud)
    dest = tmp_path / "out.bin"
    cloud["fail_seq"] = 3

    with pytest.raises(socket.timeout):
        _run(chunks, dest, progress, streams=1)
    landed = sorted(cloud["fetched"])
    cloud["fetched"].clear()

    cloud["fail_seq"] = None
    _run(chunks, dest, progress, expected_md5=_md5(whole), streams=1)

    assert landed == [0, 1, 2]
    assert sorted(cloud["fetched"]) == [3, 4, 5]  # the rest only
    assert dest.read_bytes() == whole


def test_without_a_progress_store_there_is_no_resume(tmp_path, cloud):
    chunks, whole = _setup(cloud)
    dest = tmp_path / "out.bin"
    cloud["fail_seq"] = 3
    with pytest.raises(socket.timeout):
        _run(chunks, dest, streams=1)
    cloud["fetched"].clear()

    cloud["fail_seq"] = None
    _run(chunks, dest, expected_md5=_md5(whole), streams=1)
    assert len(cloud["fetched"]) == 6  # everything again


def test_resume_survives_repeated_interruptions(tmp_path, cloud, progress):
    chunks, whole = _setup(cloud, count=8)
    dest = tmp_path / "out.bin"

    for seq in (1, 3, 5):
        cloud["fail_seq"] = seq
        with pytest.raises(socket.timeout):
            _run(chunks, dest, progress, streams=1)

    cloud["fail_seq"] = None
    _run(chunks, dest, progress, expected_md5=_md5(whole), streams=1)
    assert dest.read_bytes() == whole


def test_progress_is_discarded_for_a_different_file_at_the_same_path(
    tmp_path, cloud, progress
):
    chunks, _ = _setup(cloud, count=6)
    dest = tmp_path / "out.bin"
    cloud["fail_seq"] = 3
    with pytest.raises(socket.timeout):
        _run(chunks, dest, progress, streams=1)

    # a different file, same destination
    other, other_whole = _setup(cloud, count=4)
    for c in other:
        c_store = cloud["store"]
        c_store[c.drive_file_id] = c_store[c.drive_file_id]
    cloud["fetched"].clear()
    cloud["fail_seq"] = None
    _run(other, dest, progress, expected_md5=_md5(other_whole), streams=1)

    assert sorted(cloud["fetched"]) == [0, 1, 2, 3]  # nothing inherited


def test_a_missing_part_file_resets_recorded_progress(tmp_path, cloud, progress):
    chunks, whole = _setup(cloud)
    dest = tmp_path / "out.bin"
    cloud["fail_seq"] = 3
    with pytest.raises(socket.timeout):
        _run(chunks, dest, progress, streams=1)

    part_path_for(dest).unlink()  # the output went away; progress is meaningless
    cloud["fetched"].clear()
    cloud["fail_seq"] = None
    _run(chunks, dest, progress, expected_md5=_md5(whole), streams=1)

    assert len(cloud["fetched"]) == 6


def test_corrupt_result_clears_the_part_and_the_progress(tmp_path, cloud, progress):
    chunks, _ = _setup(cloud)
    dest = tmp_path / "out.bin"

    with pytest.raises(IntegrityError):
        _run(chunks, dest, progress, expected_md5=_md5(b"different"), streams=1)

    assert not part_path_for(dest).exists()
    assert not dest.exists()
    assert progress.completed(str(dest), FILE_ID, 6 * CHUNK) == set()


def test_progress_is_cleared_once_the_download_completes(tmp_path, cloud, progress):
    chunks, whole = _setup(cloud)
    dest = tmp_path / "out.bin"
    _run(chunks, dest, progress, expected_md5=_md5(whole), streams=1)
    assert progress.completed(str(dest), FILE_ID, len(whole)) == set()


# -- space -------------------------------------------------------------------


def test_refuses_a_download_that_cannot_fit(tmp_path, cloud, monkeypatch):
    chunks, _ = _setup(cloud)
    dest = tmp_path / "out.bin"
    tiny = shutil.disk_usage(tmp_path)._replace(free=1024)
    monkeypatch.setattr(chunk_download.shutil, "disk_usage", lambda _p: tiny)

    with pytest.raises(NotEnoughSpace, match="Not enough free space"):
        _run(chunks, dest)

    assert cloud["fetched"] == []


def test_space_check_counts_only_what_is_left(tmp_path, cloud, progress, monkeypatch):
    """A nearly-complete resume must not be refused for want of the full size."""
    chunks, whole = _setup(cloud)
    dest = tmp_path / "out.bin"
    cloud["fail_seq"] = 5
    with pytest.raises(socket.timeout):
        _run(chunks, dest, progress, streams=1)

    asked: list[int] = []
    real = chunk_download.check_space

    def spy(d, needed):
        asked.append(needed)
        return real(d, needed)

    monkeypatch.setattr(chunk_download, "check_space", spy)
    cloud["fail_seq"] = None
    _run(chunks, dest, progress, expected_md5=_md5(whole), streams=1)

    assert asked and asked[0] == CHUNK  # one chunk left, not six


# -- reporting and control ---------------------------------------------------


def test_failure_names_the_chunk(tmp_path, cloud):
    chunks, _ = _setup(cloud, count=9)
    dest = tmp_path / "out.bin"
    cloud["fail_seq"] = 4

    with pytest.raises(socket.timeout, match="chunk 5 of 9"):
        _run(chunks, dest, streams=1)


def test_reports_how_many_chunks_are_done(tmp_path, cloud):
    chunks, whole = _setup(cloud, count=4)
    dest = tmp_path / "out.bin"
    seen: list[tuple[int, int]] = []

    _run(chunks, dest, expected_md5=_md5(whole), streams=1,
         on_chunk=lambda n, total: seen.append((n, total)))

    assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_a_resumed_run_counts_from_what_already_landed(tmp_path, cloud, progress):
    chunks, whole = _setup(cloud, count=6)
    dest = tmp_path / "out.bin"
    cloud["fail_seq"] = 3
    with pytest.raises(socket.timeout):
        _run(chunks, dest, progress, streams=1)

    seen: list[tuple[int, int]] = []
    cloud["fail_seq"] = None
    _run(chunks, dest, progress, expected_md5=_md5(whole), streams=1,
         on_chunk=lambda n, total: seen.append((n, total)))

    assert seen == [(4, 6), (5, 6), (6, 6)]  # not restarting the count


def test_progress_fraction_is_monotonic_and_reaches_one(tmp_path, cloud):
    chunks, whole = _setup(cloud, count=8)
    dest = tmp_path / "out.bin"
    seen: list[float] = []

    _run(chunks, dest, expected_md5=_md5(whole), streams=1, on_progress=seen.append)

    assert seen == sorted(seen)
    assert seen[-1] == pytest.approx(1.0)


def test_cancel_stops_and_keeps_what_landed(tmp_path, cloud, progress):
    chunks, _ = _setup(cloud, count=8)
    dest = tmp_path / "out.bin"
    calls = {"n": 0}

    def stop_after_two():
        calls["n"] += 1
        return calls["n"] > 3

    with pytest.raises(Cancelled):
        _run(chunks, dest, progress, streams=1, should_stop=stop_after_two)

    assert part_path_for(dest).exists()
    assert not dest.exists()
    assert progress.completed(str(dest), FILE_ID, 8 * CHUNK)  # some landed


# -- configuration -----------------------------------------------------------


def test_stream_count_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("GSTORE_DOWNLOAD_STREAMS", raising=False)
    assert stream_count() == chunk_download.DEFAULT_STREAMS

    monkeypatch.setenv("GSTORE_DOWNLOAD_STREAMS", "8")
    assert stream_count() == 8

    monkeypatch.setenv("GSTORE_DOWNLOAD_STREAMS", "999")
    assert stream_count() == chunk_download._MAX_STREAMS

    monkeypatch.setenv("GSTORE_DOWNLOAD_STREAMS", "0")
    assert stream_count() == 1

    monkeypatch.setenv("GSTORE_DOWNLOAD_STREAMS", "banana")
    assert stream_count() == chunk_download.DEFAULT_STREAMS


# -- upgrading from the sequential downloader --------------------------------


def test_adopts_a_part_file_from_the_old_sequential_downloader(
    tmp_path, cloud, progress
):
    """Upgrading must not throw away a part-finished transfer.

    The old downloader appended in order and inferred progress from length;
    a short part file on an exact chunk boundary is a trustworthy prefix.
    """
    chunks, whole = _setup(cloud, count=6)
    dest = tmp_path / "out.bin"
    part_path_for(dest).write_bytes(whole[: 4 * CHUNK])  # as the old code left it

    _run(chunks, dest, progress, expected_md5=_md5(whole), streams=1)

    assert sorted(cloud["fetched"]) == [4, 5]  # only the missing two
    assert dest.read_bytes() == whole


def test_a_part_file_off_a_boundary_is_not_adopted(tmp_path, cloud, progress):
    chunks, whole = _setup(cloud, count=6)
    dest = tmp_path / "out.bin"
    part_path_for(dest).write_bytes(b"\x00" * (CHUNK + 11))  # torn write

    _run(chunks, dest, progress, expected_md5=_md5(whole), streams=1)

    assert len(cloud["fetched"]) == 6  # nothing trusted
    assert dest.read_bytes() == whole


def test_a_full_length_part_file_is_not_mistaken_for_a_prefix(
    tmp_path, cloud, progress
):
    """The new downloader preallocates to full size; that is not progress."""
    chunks, whole = _setup(cloud, count=6)
    dest = tmp_path / "out.bin"
    part_path_for(dest).write_bytes(b"\x00" * len(whole))

    _run(chunks, dest, progress, expected_md5=_md5(whole), streams=1)

    assert len(cloud["fetched"]) == 6
