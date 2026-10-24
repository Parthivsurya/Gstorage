"""Download and reassemble a chunked file (Phase B).

Fetches each chunk from its home account in sequence, verifying every chunk
against its stored md5, then verifies the reassembled whole file against the
original file's checksum.

Output is built in a ``.gstore-part`` file next to the destination and renamed
only once the whole-file checksum passes, so the destination path never holds
partial or unverified data. That part file is also the resume state: on a later
attempt its length says how many chunks already landed, and the download picks
up from there. Before this, any failure deleted everything — one timed-out
chunk 400 of 409 threw away 19 GB.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from google.oauth2.credentials import Credentials

from gstore.applog import get as get_logger
from gstore.drive.download import download_file
from gstore.drive.hashing import IntegrityError, verify_file
from gstore.index import Chunk

log = get_logger(__name__)

CredentialsFor = Callable[[int], Credentials]

# Suffix for the in-progress output. Visible on purpose: a half-finished 20 GB
# file should not look like a finished one.
PART_SUFFIX = ".gstore-part"

# Spare room demanded beyond the file itself, for the chunk being staged.
_HEADROOM_BYTES = 256 * 1024 * 1024


class NotEnoughSpace(RuntimeError):
    """Raised before starting when the destination cannot hold the result."""


def part_path_for(dest: str | Path) -> Path:
    """Where the in-progress download for `dest` is accumulated."""
    dest = Path(dest)
    return dest.with_name(dest.name + PART_SUFFIX)


def _resume_index(part: Path, ordered: list[Chunk]) -> int:
    """How many chunks the existing part file already holds.

    The part file is its own bookkeeping: a length that lands exactly on a
    chunk boundary means those chunks are present, in order. A length that
    does not is not trustworthy — a torn write, or chunk sizes that changed —
    so the download restarts rather than stitching onto an unknown prefix.
    """
    if not part.exists():
        return 0
    size = part.stat().st_size
    if size == 0:
        return 0
    cumulative = 0
    for index, chunk in enumerate(ordered):
        if cumulative == size:
            return index
        cumulative += chunk.size_bytes
    if cumulative == size:
        return len(ordered)
    log.warning(
        "part file %s is %d bytes, which is not a chunk boundary — restarting",
        part.name,
        size,
    )
    return 0


def check_space(dest: str | Path, needed_bytes: int) -> None:
    """Refuse a download that cannot fit, before any of it is transferred.

    Finding out 19 GB in is an expensive way to learn the disk was full.
    """
    dest = Path(dest)
    target = dest.parent if dest.parent.exists() else Path.home()
    free = shutil.disk_usage(target).free
    required = needed_bytes + _HEADROOM_BYTES
    if free < required:
        raise NotEnoughSpace(
            f"Not enough free space in {target}: need about "
            f"{required / 1024**3:.1f} GB, {free / 1024**3:.1f} GB available."
        )


def download_chunks(
    chunks: list[Chunk],
    credentials_for: CredentialsFor,
    dest_path: str | Path,
    expected_md5: str | None = None,
    on_progress: Callable[[float], None] | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> Path:
    """Reassemble `chunks` into `dest_path`; verify each chunk and the whole.

    Resumes from a previous attempt's part file when one is present. Raises
    :class:`IntegrityError` if the reassembled file fails its checksum, and
    :class:`NotEnoughSpace` if the destination could not hold the result.

    `on_chunk` receives (chunk number, total) as each one starts, so the UI can
    say which of 409 is in flight rather than only a percentage.
    """
    dest = Path(dest_path)
    part = part_path_for(dest)
    ordered = sorted(chunks, key=lambda c: c.sequence)
    total = sum(c.size_bytes for c in ordered) or 1

    start_at = _resume_index(part, ordered)
    done = sum(c.size_bytes for c in ordered[:start_at])
    check_space(dest, total - done)

    if start_at:
        log.info(
            "resuming %s from chunk %d/%d (%.2f GB already on disk)",
            dest.name, start_at, len(ordered), done / 1024**3,
        )
    else:
        log.info("downloading %s: %d chunks, %.2f GB",
                 dest.name, len(ordered), total / 1024**3)

    tmpdir = Path(tempfile.mkdtemp(prefix="gstore-chunks-"))
    try:
        # Append: everything before start_at is already in the part file.
        with open(part, "r+b" if start_at else "wb") as out:
            out.seek(done)
            out.truncate()
            for index in range(start_at, len(ordered)):
                chunk = ordered[index]
                if should_stop is not None and should_stop():
                    from gstore.drive.rate_limit import Cancelled

                    raise Cancelled("download cancelled")
                if on_chunk is not None:
                    on_chunk(index + 1, len(ordered))

                staged = tmpdir / f"part-{chunk.sequence:05d}"
                try:
                    download_file(
                        credentials_for(chunk.account_id),
                        chunk.drive_file_id,
                        staged,
                        on_progress=(
                            lambda frac, base=done, length=chunk.size_bytes: (
                                on_progress((base + frac * length) / total)
                                if on_progress is not None
                                else None
                            )
                        ),
                        expected_md5=chunk.md5,
                    )
                except Exception as exc:
                    # Name the chunk. "timed out" on its own tells nobody
                    # anything, least of all which of 409 to look at.
                    log.error(
                        "chunk %d/%d (sequence %d, account %s) failed: %s: %s",
                        index + 1, len(ordered), chunk.sequence,
                        chunk.account_id, type(exc).__name__, exc,
                    )
                    raise type(exc)(
                        f"chunk {index + 1} of {len(ordered)} failed: {exc}"
                    ) from exc

                with open(staged, "rb") as pf:
                    shutil.copyfileobj(pf, out)
                out.flush()
                staged.unlink(missing_ok=True)
                done += chunk.size_bytes

        if expected_md5:
            verify_file(part, expected_md5)

        part.replace(dest)  # atomic on the same filesystem
        log.info("downloaded %s (%.2f GB)", dest.name, total / 1024**3)
        if on_progress is not None:
            on_progress(1.0)
        return dest
    except IntegrityError:
        # The bytes on disk are wrong, not merely incomplete — keeping them
        # would let a later attempt "resume" onto corrupt data.
        log.error("whole-file checksum failed for %s; discarding", dest.name)
        part.unlink(missing_ok=True)
        raise
    except Exception:
        # Incomplete, not corrupt: the part file stays so the next attempt
        # resumes instead of starting the whole transfer again.
        log.warning(
            "download of %s interrupted at %.2f GB of %.2f GB; part kept for resume",
            dest.name, done / 1024**3, total / 1024**3,
        )
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
