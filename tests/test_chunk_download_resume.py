"""Tests for chunked-download resume, space checks and failure reporting.

Uploads have staged every finished chunk since Phase B; downloads threw
everything away. A single timed-out chunk — 400 of 409, say — deleted the
output and started a 20 GB transfer from zero, and the dialog said only
"timed out", naming neither the chunk nor how far it had got.

These pin the three properties that fixes that: an interrupted download leaves
a part file that the next attempt continues from, a download that cannot fit
is refused before any of it is fetched, and a failure names the chunk.
"""

from __future__ import annotations

import hashlib
import shutil
import socket

import pytest

import gstore.drive.chunk_download as chunk_download
from gstore.drive.chunk_download import (
    NotEnoughSpace,
    download_chunks,
    part_path_for,
)
from gstore.drive.hashing import IntegrityError
from gstore.index import Chunk

MB = 1024 * 1024
CHUNK = 64 * 1024


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _chunks(payloads: list[bytes]) -> list[Chunk]:
    return [
        Chunk(
            id=i,
            file_id=1,
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
    """Stub for download_file, with a switch to fail on the Nth call."""

    state = {"store": {}, "fail_on": None, "calls": 0, "fetched": []}

    def fake_download_file(credentials, drive_file_id, dest, on_progress=None,
                           expected_md5=None):
        state["calls"] += 1
        if state["fail_on"] is not None and state["calls"] == state["fail_on"]:
            raise socket.timeout("timed out")
        data = state["store"][drive_file_id]
        from pathlib import Path

        Path(dest).write_bytes(data)
        state["fetched"].append(drive_file_id)
        if on_progress:
            on_progress(1.0)
        return Path(dest)

    monkeypatch.setattr(chunk_download, "download_file", fake_download_file)
    return state


def _creds_for(account_id):
    return object()


def _setup(cloud, count=6, size=CHUNK):
    payloads = [bytes([i]) * size for i in range(count)]
    chunks = _chunks(payloads)
    for c, p in zip(chunks, payloads):
        cloud["store"][c.drive_file_id] = p
    return chunks, b"".join(payloads)


# -- the happy path ----------------------------------------------------------


def test_reassembles_and_renames_only_on_success(tmp_path, cloud):
    chunks, whole = _setup(cloud)
    dest = tmp_path / "out.bin"

    download_chunks(chunks, _creds_for, dest, expected_md5=_md5(whole))

    assert dest.read_bytes() == whole
    assert not part_path_for(dest).exists()  # part renamed away


def test_destination_never_holds_partial_data(tmp_path, cloud):
    chunks, _ = _setup(cloud)
    dest = tmp_path / "out.bin"
    cloud["fail_on"] = 3

    with pytest.raises(socket.timeout):
        download_chunks(chunks, _creds_for, dest)

    assert not dest.exists()  # only the .gstore-part file exists
    assert part_path_for(dest).exists()


# -- resume ------------------------------------------------------------------


def test_interrupted_download_keeps_a_part_file(tmp_path, cloud):
    chunks, _ = _setup(cloud)
    dest = tmp_path / "out.bin"
    cloud["fail_on"] = 4  # dies on the 4th chunk

    with pytest.raises(socket.timeout):
        download_chunks(chunks, _creds_for, dest)

    part = part_path_for(dest)
    assert part.stat().st_size == 3 * CHUNK  # three chunks survived


def test_resume_fetches_only_the_missing_chunks(tmp_path, cloud):
    chunks, whole = _setup(cloud)
    dest = tmp_path / "out.bin"
    cloud["fail_on"] = 4

    with pytest.raises(socket.timeout):
        download_chunks(chunks, _creds_for, dest)
    fetched_first = len(cloud["fetched"])

    cloud["fail_on"] = None
    download_chunks(chunks, _creds_for, dest, expected_md5=_md5(whole))

    assert fetched_first == 3
    assert len(cloud["fetched"]) == 6  # only chunks 4-6 on the retry
    assert dest.read_bytes() == whole


def test_resume_survives_several_interruptions(tmp_path, cloud):
    chunks, whole = _setup(cloud, count=6)
    dest = tmp_path / "out.bin"

    for fail_at in (2, 2, 2):  # dies repeatedly, inches forward each time
        cloud["calls"] = 0
        cloud["fail_on"] = fail_at
        with pytest.raises(socket.timeout):
            download_chunks(chunks, _creds_for, dest)

    cloud["calls"] = 0
    cloud["fail_on"] = None
    download_chunks(chunks, _creds_for, dest, expected_md5=_md5(whole))
    assert dest.read_bytes() == whole


def test_a_part_file_off_a_chunk_boundary_restarts(tmp_path, cloud):
    """A torn write must not be stitched onto; its length proves nothing."""
    chunks, whole = _setup(cloud)
    dest = tmp_path / "out.bin"
    part = part_path_for(dest)
    part.write_bytes(b"\x00" * (CHUNK + 7))  # not a boundary

    download_chunks(chunks, _creds_for, dest, expected_md5=_md5(whole))

    assert dest.read_bytes() == whole
    assert len(cloud["fetched"]) == 6  # everything re-fetched, nothing trusted


def test_corrupt_result_discards_the_part_rather_than_resuming_onto_it(
    tmp_path, cloud
):
    chunks, _ = _setup(cloud)
    dest = tmp_path / "out.bin"

    with pytest.raises(IntegrityError):
        download_chunks(chunks, _creds_for, dest, expected_md5=_md5(b"something else"))

    assert not part_path_for(dest).exists()
    assert not dest.exists()


# -- space -------------------------------------------------------------------


def test_refuses_a_download_that_cannot_fit(tmp_path, cloud, monkeypatch):
    chunks, _ = _setup(cloud)
    dest = tmp_path / "out.bin"
    tiny = shutil.disk_usage(tmp_path)._replace(free=1024)
    monkeypatch.setattr(chunk_download.shutil, "disk_usage", lambda _p: tiny)

    with pytest.raises(NotEnoughSpace, match="Not enough free space"):
        download_chunks(chunks, _creds_for, dest)

    assert cloud["fetched"] == []  # refused before fetching anything


def test_space_check_counts_only_what_is_left_to_fetch(tmp_path, cloud, monkeypatch):
    """Resuming a nearly-complete download must not demand the full size."""
    chunks, whole = _setup(cloud)
    dest = tmp_path / "out.bin"
    part = part_path_for(dest)
    part.write_bytes(whole[: 5 * CHUNK])  # only one chunk left

    seen = {}
    real = shutil.disk_usage

    def spy(p):
        usage = real(p)
        seen["free_needed_under"] = usage.free
        return usage

    monkeypatch.setattr(chunk_download.shutil, "disk_usage", spy)
    download_chunks(chunks, _creds_for, dest, expected_md5=_md5(whole))
    assert len(cloud["fetched"]) == 1


# -- error reporting ---------------------------------------------------------


def test_failure_names_the_chunk(tmp_path, cloud):
    """"timed out" alone told nobody which of 409 chunks to look at."""
    chunks, _ = _setup(cloud, count=9)
    dest = tmp_path / "out.bin"
    cloud["fail_on"] = 5

    with pytest.raises(socket.timeout, match="chunk 5 of 9"):
        download_chunks(chunks, _creds_for, dest)


def test_reports_the_chunk_in_flight(tmp_path, cloud):
    chunks, whole = _setup(cloud, count=4)
    dest = tmp_path / "out.bin"
    seen: list[tuple[int, int]] = []

    download_chunks(
        chunks, _creds_for, dest, expected_md5=_md5(whole), on_chunk=lambda i, n: seen.append((i, n))
    )

    assert seen == [(1, 4), (2, 4), (3, 4), (4, 4)]


def test_resumed_run_announces_from_the_right_chunk(tmp_path, cloud):
    chunks, whole = _setup(cloud, count=6)
    dest = tmp_path / "out.bin"
    cloud["fail_on"] = 4
    with pytest.raises(socket.timeout):
        download_chunks(chunks, _creds_for, dest)

    seen: list[tuple[int, int]] = []
    cloud["fail_on"] = None
    download_chunks(
        chunks, _creds_for, dest, expected_md5=_md5(whole),
        on_chunk=lambda i, n: seen.append((i, n)),
    )
    assert seen == [(4, 6), (5, 6), (6, 6)]  # not restarting the count at 1


def test_cancel_stops_and_keeps_the_part(tmp_path, cloud):
    from gstore.drive.rate_limit import Cancelled

    chunks, _ = _setup(cloud, count=6)
    dest = tmp_path / "out.bin"
    calls = {"n": 0}

    def stop_after_two():
        calls["n"] += 1
        return calls["n"] > 2

    with pytest.raises(Cancelled):
        download_chunks(chunks, _creds_for, dest, should_stop=stop_after_two)

    assert part_path_for(dest).exists()
    assert not dest.exists()
