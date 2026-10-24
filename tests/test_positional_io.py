"""Tests for positional file I/O (gstore.drive.positional_io).

Parallel transfers read and write one file from several threads at once, each
at its own offset. The first implementation called os.pread/os.pwrite
directly — which do not exist on Windows, so every parallel transfer would
have crashed there with AttributeError. macOS and Linux could never have
caught it; the first Windows CI build did.

The fallback therefore has to be exercised on every platform, not only the
one that uses it. These force it on and check it behaves identically,
including under the concurrency it exists for.
"""

from __future__ import annotations

import os
import threading

import pytest

from gstore.drive import positional_io
from gstore.drive.positional_io import open_binary, pread, pwrite


@pytest.fixture(params=["native", "fallback"])
def mode(request, monkeypatch):
    """Run every test both ways: the Unix syscalls and the Windows fallback."""
    if request.param == "fallback":
        monkeypatch.setattr(positional_io, "HAS_POSITIONAL_IO", False)
    return request.param


@pytest.fixture
def datafile(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(bytes(range(256)) * 64)  # 16 KiB of a known pattern
    return path


def test_reads_the_requested_range(mode, datafile):
    fd = open_binary(datafile)
    try:
        assert pread(fd, 10, 0) == bytes(range(10))
        assert pread(fd, 4, 256) == bytes(range(4))  # pattern repeats
    finally:
        os.close(fd)


def test_reading_does_not_move_the_file_position(mode, datafile):
    """The whole point: a worker's read must not disturb another's."""
    fd = open_binary(datafile)
    try:
        os.lseek(fd, 100, os.SEEK_SET)
        pread(fd, 10, 4000)
        assert os.lseek(fd, 0, os.SEEK_CUR) in (100, 4010)
        # Native pread leaves it at 100; the locked fallback moves it. Either
        # is fine — what matters is that concurrent callers cannot interleave,
        # which the test below proves.
    finally:
        os.close(fd)


def test_writes_land_at_the_offset(mode, tmp_path):
    path = tmp_path / "out.bin"
    path.write_bytes(b"\x00" * 100)
    fd = open_binary(path, os.O_RDWR)
    try:
        pwrite(fd, b"abc", 10)
        pwrite(fd, b"xyz", 50)
    finally:
        os.close(fd)
    blob = path.read_bytes()
    assert blob[10:13] == b"abc"
    assert blob[50:53] == b"xyz"
    assert blob[0:10] == b"\x00" * 10  # nothing else touched


def test_a_short_file_reads_short_rather_than_raising(mode, datafile):
    fd = open_binary(datafile)
    try:
        assert pread(fd, 5000, datafile.stat().st_size - 10) == datafile.read_bytes()[-10:]
    finally:
        os.close(fd)


# -- the reason it exists ----------------------------------------------------


def test_concurrent_reads_never_return_another_thread_s_bytes(mode, tmp_path):
    """A bare seek-then-read would interleave and silently cross the wires."""
    chunk = 4096
    threads = 8
    path = tmp_path / "big.bin"
    path.write_bytes(b"".join(bytes([i]) * chunk for i in range(threads)))

    fd = open_binary(path)
    wrong: list[int] = []
    barrier = threading.Barrier(threads)

    def worker(index: int) -> None:
        barrier.wait()  # maximise overlap
        for _ in range(40):
            got = pread(fd, chunk, index * chunk)
            if got != bytes([index]) * chunk:
                wrong.append(index)
                return

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    os.close(fd)

    assert wrong == []


def test_concurrent_writes_do_not_overwrite_each_other(mode, tmp_path):
    chunk = 4096
    threads = 8
    path = tmp_path / "out.bin"
    path.write_bytes(b"\x00" * (chunk * threads))

    fd = open_binary(path, os.O_RDWR)
    barrier = threading.Barrier(threads)

    def worker(index: int) -> None:
        barrier.wait()
        pwrite(fd, bytes([index + 1]) * chunk, index * chunk)

    workers = [threading.Thread(target=worker, args=(i,)) for i in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    os.close(fd)

    blob = path.read_bytes()
    for i in range(threads):
        assert blob[i * chunk:(i + 1) * chunk] == bytes([i + 1]) * chunk


# -- platform cover ----------------------------------------------------------


def test_the_fallback_is_used_when_the_syscalls_are_missing(monkeypatch, datafile):
    """Windows has neither; the helper must not reach for them."""
    monkeypatch.setattr(positional_io, "HAS_POSITIONAL_IO", False)
    monkeypatch.delattr(os, "pread", raising=False)
    monkeypatch.delattr(os, "pwrite", raising=False)

    fd = open_binary(datafile)
    try:
        assert pread(fd, 8, 0) == bytes(range(8))  # would AttributeError before
    finally:
        os.close(fd)
