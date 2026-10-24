"""Positional file reads and writes that work on Windows too.

Parallel transfers need several workers touching one file at once, each at its
own offset. On Unix ``os.pread``/``os.pwrite`` do exactly that: they take the
offset as an argument and never touch the descriptor's shared position, so
no two workers can interfere.

**Those two calls do not exist on Windows.** Using them directly made every
parallel transfer crash there with AttributeError — caught by CI on the first
Windows build, and invisible on macOS and Linux.

So Windows gets the only other correct option: a lock around seek-then-act.
That serialises the syscall, not the transfer — workers hold it for a memory
copy while the network, which is the actual bottleneck, stays fully parallel.
Dropping the lock and seeking per worker would be a data race: two threads
would interleave seek and read and silently return each other's bytes.
"""

from __future__ import annotations

import os
import threading

HAS_POSITIONAL_IO = hasattr(os, "pread") and hasattr(os, "pwrite")

# Windows opens descriptors in TEXT mode unless told otherwise, translating
# CRLF on the way through — which silently corrupts binary data and every
# checksum computed over it. O_BINARY does not exist elsewhere, where the
# behaviour is already binary.
_BINARY = getattr(os, "O_BINARY", 0)


def open_binary(path, flags: int = os.O_RDONLY) -> int:
    """Open a descriptor for binary positional I/O, on any platform."""
    return os.open(path, flags | _BINARY)

# One lock for the whole process. Contention is irrelevant: it is held only
# for a read or write into a buffer, never across a network request.
_fallback_lock = threading.Lock()


def pread(fd: int, size: int, offset: int) -> bytes:
    """Read `size` bytes at `offset` without disturbing the file position."""
    if HAS_POSITIONAL_IO:
        return os.pread(fd, size, offset)
    with _fallback_lock:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.read(fd, size)


def pwrite(fd: int, data: bytes, offset: int) -> int:
    """Write `data` at `offset` without disturbing the file position."""
    if HAS_POSITIONAL_IO:
        return os.pwrite(fd, data, offset)
    with _fallback_lock:
        os.lseek(fd, offset, os.SEEK_SET)
        return os.write(fd, data)
