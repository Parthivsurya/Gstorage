"""Download and reassemble a chunked file (Phase B).

Fetches each chunk from its home account in sequence, verifying every chunk
against its stored md5, then verifies the reassembled whole file against the
original file's checksum. Any integrity failure removes the partial output so
corrupt data never lands on disk.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from google.oauth2.credentials import Credentials

from gstore.drive.download import download_file
from gstore.drive.hashing import IntegrityError, verify_file
from gstore.index import Chunk

CredentialsFor = Callable[[int], Credentials]


def download_chunks(
    chunks: list[Chunk],
    credentials_for: CredentialsFor,
    dest_path: str | Path,
    expected_md5: str | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Reassemble `chunks` into `dest_path`; verify each chunk and the whole.

    Raises :class:`IntegrityError` (removing the partial file) if a chunk or the
    reassembled file fails verification.
    """
    dest = Path(dest_path)
    ordered = sorted(chunks, key=lambda c: c.sequence)
    total = sum(c.size_bytes for c in ordered) or 1
    done = 0
    tmpdir = Path(tempfile.mkdtemp(prefix="gstore-chunks-"))

    try:
        with open(dest, "wb") as out:
            for chunk in ordered:
                part = tmpdir / f"part-{chunk.sequence:05d}"
                # Per-chunk download verifies the chunk's md5 and cleans up the
                # part on mismatch; report progress scaled into the whole.
                download_file(
                    credentials_for(chunk.account_id),
                    chunk.drive_file_id,
                    part,
                    on_progress=(
                        lambda frac, base=done, length=chunk.size_bytes: (
                            on_progress((base + frac * length) / total)
                            if on_progress is not None
                            else None
                        )
                    ),
                    expected_md5=chunk.md5,
                )
                with open(part, "rb") as pf:
                    shutil.copyfileobj(pf, out)
                part.unlink(missing_ok=True)
                done += chunk.size_bytes

        if expected_md5:
            try:
                verify_file(dest, expected_md5)
            except IntegrityError:
                dest.unlink(missing_ok=True)
                raise

        if on_progress is not None:
            on_progress(1.0)
        return dest
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
