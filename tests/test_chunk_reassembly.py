"""Tests for chunked-download reassembly (gstore.drive.chunk_download).

Reassembly is a data-loss path: chunks are fetched from several accounts and
stitched back into one file. These tests exercise ordering, per-chunk and
whole-file integrity, and the guarantee that no partial/corrupt output is left
on disk when anything fails. The network is stubbed — `download_file` is
replaced with a fake backed by an in-memory {drive_file_id: bytes} store that
faithfully mirrors the real function's contract (write, verify, clean up on
mismatch).
"""

from __future__ import annotations

import hashlib

import pytest

import gstore.drive.chunk_download as chunk_download
from gstore.drive.chunk_download import download_chunks
from gstore.drive.hashing import IntegrityError
from gstore.drive.placement import chunk_ranges
from gstore.index import Chunk


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _make_chunk(sequence: int, drive_id: str, md5: str, size: int) -> Chunk:
    """A Chunk row as list_chunks would return it (account present/active)."""
    return Chunk(
        id=sequence,
        file_id=1,
        sequence=sequence,
        account_id=100 + sequence,  # distinct account per chunk
        account_email=f"acct{sequence}@example.com",
        account_status="active",
        drive_file_id=drive_id,
        md5=md5,
        size_bytes=size,
    )


@pytest.fixture
def fake_download(monkeypatch):
    """Install a fake _fetch_chunk backed by an in-memory byte store.

    Returns the mutable store dict; tests populate it with
    {drive_file_id: bytes}. The fake mirrors the real worker: write the chunk
    into the output file at its offset, verify it against the chunk's md5, and
    raise IntegrityError on a mismatch.
    """
    store: dict[str, bytes] = {}

    def _fake(credentials, chunk, fd, offset, should_stop=None):
        import os as _os

        if should_stop is not None and should_stop():
            from gstore.drive.rate_limit import Cancelled

            raise Cancelled("download cancelled")
        data = store[chunk.drive_file_id]
        if chunk.md5 and _md5(data) != chunk.md5:
            raise IntegrityError(f"chunk {chunk.sequence}: checksum mismatch")
        _os.pwrite(fd, data, offset)
        return len(data)

    monkeypatch.setattr(chunk_download, "_fetch_chunk", _fake)
    return store


def _creds_for(account_id: int) -> object:
    return object()  # the fake download ignores credentials


def test_reassembles_chunks_in_sequence_order(fake_download, tmp_path):
    parts = [b"A" * 10, b"B" * 10, b"C" * 5]
    whole = b"".join(parts)
    for i, data in enumerate(parts):
        fake_download[f"drive-{i}"] = data
    # Feed the chunks out of order — reassembly must sort by sequence.
    chunks = [
        _make_chunk(2, "drive-2", _md5(parts[2]), len(parts[2])),
        _make_chunk(0, "drive-0", _md5(parts[0]), len(parts[0])),
        _make_chunk(1, "drive-1", _md5(parts[1]), len(parts[1])),
    ]

    dest = tmp_path / "out.bin"
    result = download_chunks(chunks, _creds_for, dest, expected_md5=_md5(whole))

    assert result == dest
    assert dest.read_bytes() == whole


def test_single_chunk_file(fake_download, tmp_path):
    data = b"only-one-piece"
    fake_download["d0"] = data
    chunks = [_make_chunk(0, "d0", _md5(data), len(data))]

    dest = tmp_path / "one.bin"
    download_chunks(chunks, _creds_for, dest, expected_md5=_md5(data))

    assert dest.read_bytes() == data


def test_each_chunk_fetched_from_its_own_account(fake_download, tmp_path):
    parts = [b"x" * 4, b"y" * 4, b"z" * 4]
    for i, data in enumerate(parts):
        fake_download[f"d{i}"] = data
    chunks = [_make_chunk(i, f"d{i}", _md5(parts[i]), len(parts[i])) for i in range(3)]

    seen: list[int] = []

    def creds_for(account_id: int) -> object:
        seen.append(account_id)
        return object()

    download_chunks(chunks, creds_for, tmp_path / "o.bin")

    # credentials requested once per chunk, for that chunk's account.
    assert seen == [100, 101, 102]


def test_bad_chunk_checksum_raises_and_removes_output(fake_download, tmp_path):
    good, bad = b"G" * 10, b"corrupt!!!"
    fake_download["d0"] = good
    fake_download["d1"] = bad  # bytes won't match the recorded md5
    chunks = [
        _make_chunk(0, "d0", _md5(good), len(good)),
        _make_chunk(1, "d1", _md5(b"B" * 10), 10),  # recorded md5 != stored bytes
    ]

    dest = tmp_path / "out.bin"
    with pytest.raises(IntegrityError):
        download_chunks(chunks, _creds_for, dest, expected_md5=_md5(good + b"B" * 10))

    # A per-chunk failure must not leave the partially-assembled file behind.
    assert not dest.exists()


def test_whole_file_checksum_mismatch_raises_and_removes_output(fake_download, tmp_path):
    # Every chunk verifies individually, but the caller's whole-file md5 is wrong
    # (e.g. a stale index entry) — reassembly must reject and clean up.
    parts = [b"1" * 8, b"2" * 8]
    for i, data in enumerate(parts):
        fake_download[f"d{i}"] = data
    chunks = [_make_chunk(i, f"d{i}", _md5(parts[i]), len(parts[i])) for i in range(2)]

    dest = tmp_path / "out.bin"
    with pytest.raises(IntegrityError):
        download_chunks(chunks, _creds_for, dest, expected_md5=_md5(b"totally-different"))

    assert not dest.exists()


def test_progress_is_monotonic_and_reaches_one(fake_download, tmp_path):
    parts = [b"a" * 20, b"b" * 20, b"c" * 20]
    for i, data in enumerate(parts):
        fake_download[f"d{i}"] = data
    chunks = [_make_chunk(i, f"d{i}", _md5(parts[i]), len(parts[i])) for i in range(3)]

    seen: list[float] = []
    download_chunks(
        chunks, _creds_for, tmp_path / "o.bin", on_progress=seen.append
    )

    assert seen, "expected progress callbacks"
    assert all(0.0 <= f <= 1.0 for f in seen)
    assert seen == sorted(seen)  # non-decreasing
    assert seen[-1] == pytest.approx(1.0)


def test_missing_expected_md5_skips_whole_file_check(fake_download, tmp_path):
    # Legacy files predating checksums have no whole-file md5; reassembly should
    # still succeed on the per-chunk guarantees alone.
    parts = [b"p" * 6, b"q" * 6]
    for i, data in enumerate(parts):
        fake_download[f"d{i}"] = data
    chunks = [_make_chunk(i, f"d{i}", _md5(parts[i]), len(parts[i])) for i in range(2)]

    dest = tmp_path / "o.bin"
    download_chunks(chunks, _creds_for, dest, expected_md5=None)

    assert dest.read_bytes() == b"".join(parts)


@pytest.mark.parametrize("size", [0 + 1, 50, 149, 150, 151, 500])
def test_split_then_reassemble_round_trips(fake_download, tmp_path, size):
    """The inverse property: chunk_ranges then download_chunks == original.

    Splitting must be exhaustive and ordered so reassembly reproduces the exact
    original bytes for any size and chunk boundary.
    """
    original = bytes((i % 256) for i in range(size))
    chunk_size = 50  # force multiple chunks with a small last remainder

    ranges = chunk_ranges(size, chunk_size)
    chunks = []
    for sequence, offset, length in ranges:
        piece = original[offset : offset + length]
        drive_id = f"d{sequence}"
        fake_download[drive_id] = piece
        chunks.append(_make_chunk(sequence, drive_id, _md5(piece), length))

    dest = tmp_path / "round.bin"
    download_chunks(chunks, _creds_for, dest, expected_md5=_md5(original))

    assert dest.read_bytes() == original
