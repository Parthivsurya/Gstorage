"""Content hashing for integrity verification.

Drive returns an MD5 (`md5Checksum`) for uploaded binary files, so MD5 is what
we compare against to confirm an upload landed intact. Hashes stream the file in
fixed blocks — never loading the whole thing into memory — so this stays cheap
even for multi-gigabyte files. Chunking (Phase 6) reuses these helpers per chunk.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from gstore.drive.rate_limit import Cancelled

_READ_BYTES = 1024 * 1024  # 1 MB read blocks


class IntegrityError(RuntimeError):
    """Raised when content fails an MD5 checksum comparison."""


def md5_of_file(
    path: str | Path,
    on_progress: Callable[[float], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """Return the hex MD5 digest of a file's contents, read in blocks.

    Hashing a multi-gigabyte file takes long enough to look like a hang, so
    `on_progress` receives a 0.0-1.0 fraction as it goes and `should_stop`
    lets a user cancel land during the read rather than after it.
    """
    digest = hashlib.md5()
    total = Path(path).stat().st_size or 1
    read = 0
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_READ_BYTES), b""):
            if should_stop is not None and should_stop():
                raise Cancelled("cancelled while checksumming")
            digest.update(block)
            read += len(block)
            if on_progress is not None:
                on_progress(read / total)
    return digest.hexdigest()


def verify_file(path: str | Path, expected_md5: str) -> str:
    """Check `path`'s MD5 against `expected_md5`; return the computed digest.

    Raises :class:`IntegrityError` on mismatch. Callers with no expected hash
    (e.g. legacy files predating checksums) should skip calling this.
    """
    actual = md5_of_file(path)
    if actual != expected_md5:
        raise IntegrityError(
            f"Checksum mismatch for {Path(path).name}: "
            f"expected {expected_md5}, got {actual}."
        )
    return actual
