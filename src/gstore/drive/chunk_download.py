"""Download and reassemble a chunked file (Phase B), several chunks at a time.

Chunks are fetched concurrently and written straight into the output file at
their own offsets. Each is verified against its stored md5 as it streams, and
the reassembled whole is verified against the original file's checksum before
the output is renamed into place.

Concurrency is the point. Measured against Drive over 5G, one stream was held
to ~1.3 Mbps regardless of an 80 Mb/s link — latency and per-connection limits,
not capacity — while eight streams moved eight times the data in the same wall
time. Sequential chunks turned a 20 GB download into hours of mostly waiting.

That forces the resume state into the database: workers finish out of order, so
the output file's length no longer says how far the download got. The file is
preallocated to full size and filled in at offsets, and `download_progress`
records which sequences actually landed.

Output is built in a sibling ``.gstore-part`` file and renamed only once the
whole-file checksum passes, so the destination never holds partial data.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Callable

from google.oauth2.credentials import Credentials
from googleapiclient.http import MediaIoBaseDownload

from gstore.applog import get as get_logger
from gstore.drive.hashing import IntegrityError
from gstore.drive.positional_io import pwrite
from gstore.drive.rate_limit import Cancelled, execute_with_backoff
from gstore.drive.service import drive_service
from gstore.index import Chunk

log = get_logger(__name__)

CredentialsFor = Callable[[int], Credentials]

PART_SUFFIX = ".gstore-part"

# Transfer granularity within one chunk.
_HTTP_CHUNK_BYTES = 4 * 1024 * 1024

# Spare room demanded beyond the file itself.
_HEADROOM_BYTES = 256 * 1024 * 1024

# How many chunks to fetch at once. Four is deliberately short of what the
# link can take — throughput still scaled at eight in testing — because every
# stream spends Drive API quota and shares a per-user rate limit. Raise it with
# GSTORE_DOWNLOAD_STREAMS when the quota budget allows.
DEFAULT_STREAMS = 4
_STREAMS_ENV = "GSTORE_DOWNLOAD_STREAMS"
_MAX_STREAMS = 16


class NotEnoughSpace(RuntimeError):
    """Raised before starting when the destination cannot hold the result."""


def stream_count() -> int:
    """Concurrent chunk downloads, from the environment or the default."""
    raw = os.environ.get(_STREAMS_ENV)
    if not raw:
        return DEFAULT_STREAMS
    try:
        return max(1, min(int(raw), _MAX_STREAMS))
    except ValueError:
        log.warning("%s=%r is not a number; using %d", _STREAMS_ENV, raw, DEFAULT_STREAMS)
        return DEFAULT_STREAMS


def part_path_for(dest: str | Path) -> Path:
    """Where the in-progress download for `dest` is accumulated."""
    dest = Path(dest)
    return dest.with_name(dest.name + PART_SUFFIX)


def check_space(dest: str | Path, needed_bytes: int) -> None:
    """Refuse a download that cannot fit, before any of it is transferred."""
    dest = Path(dest)
    target = dest.parent if dest.parent.exists() else Path.home()
    free = shutil.disk_usage(target).free
    required = needed_bytes + _HEADROOM_BYTES
    if free < required:
        raise NotEnoughSpace(
            f"Not enough free space in {target}: need about "
            f"{required / 1024**3:.1f} GB, {free / 1024**3:.1f} GB available."
        )


class _OffsetWriter:
    """Writes a chunk's stream into the output file at a fixed offset.

    MediaIoBaseDownload writes sequentially from zero, so this shifts every
    write by the chunk's base offset. :func:`gstore.drive.positional_io.pwrite`
    is positional and does not touch the descriptor's shared file offset,
    which is what makes several workers writing different regions of one file
    safe. (Windows has no os.pwrite, so that helper falls back to a locked
    seek-then-write there.)

    It also hashes as it writes, so verifying a chunk costs no extra read.
    """

    def __init__(self, fd: int, base_offset: int) -> None:
        self._fd = fd
        self._base = base_offset
        self._pos = 0
        self._digest = hashlib.md5()

    def write(self, data: bytes) -> int:
        written = pwrite(self._fd, data, self._base + self._pos)
        self._digest.update(data[:written])
        self._pos += written
        return written

    @property
    def md5(self) -> str:
        return self._digest.hexdigest()

    @property
    def written(self) -> int:
        return self._pos


def _fetch_chunk(
    credentials: Credentials,
    chunk: Chunk,
    fd: int,
    offset: int,
    should_stop: Callable[[], bool] | None,
) -> int:
    """Stream one chunk into the output at `offset`; verify it; return its size."""
    service = drive_service(credentials)
    request = service.files().get_media(fileId=chunk.drive_file_id)
    writer = _OffsetWriter(fd, offset)
    downloader = MediaIoBaseDownload(writer, request, chunksize=_HTTP_CHUNK_BYTES)

    done = False
    while not done:
        if should_stop is not None and should_stop():
            raise Cancelled("download cancelled")
        _status, done = execute_with_backoff(
            downloader.next_chunk, should_stop=should_stop
        )

    if writer.written != chunk.size_bytes:
        raise IntegrityError(
            f"chunk {chunk.sequence} is {writer.written} bytes, "
            f"expected {chunk.size_bytes}"
        )
    if chunk.md5 and writer.md5 != chunk.md5:
        raise IntegrityError(
            f"chunk {chunk.sequence} failed its checksum: "
            f"expected {chunk.md5}, got {writer.md5}"
        )
    return writer.written


def _adopt_sequential_part(
    part: Path, ordered: list[Chunk], total: int, store, key: str, file_id: int
) -> set[int]:
    """Carry over a part file written by the older sequential downloader.

    That version appended chunks in order and inferred progress from the file's
    length, so a short part file whose length lands exactly on a chunk boundary
    is a trustworthy prefix. Without this, upgrading would silently discard a
    part-finished transfer — 5.4 GB of one, in the case that prompted it.

    A length that is not on a boundary proves nothing and is ignored.
    """
    size = part.stat().st_size
    if size <= 0 or size >= total:
        return set()
    cumulative = 0
    adopted: list[Chunk] = []
    for chunk in ordered:
        if cumulative == size:
            break
        cumulative += chunk.size_bytes
        adopted.append(chunk)
    if cumulative != size or not adopted:
        return set()
    for chunk in adopted:
        store.record(key, file_id, total, chunk.sequence, chunk.size_bytes)
    log.info(
        "adopted %d chunks (%.2f GB) from a part file written by the "
        "sequential downloader", len(adopted), size / 1024**3,
    )
    return {c.sequence for c in adopted}


def download_chunks(
    chunks: list[Chunk],
    credentials_for: CredentialsFor,
    dest_path: str | Path,
    expected_md5: str | None = None,
    on_progress: Callable[[float], None] | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    progress_store=None,
    file_id: int = 0,
    streams: int | None = None,
) -> Path:
    """Reassemble `chunks` into `dest_path`, several at a time.

    `progress_store` is a :class:`~gstore.index.download_progress.DownloadProgress`.
    It is written only from this thread — SQLite connections are bound to the
    thread that made them — so workers hand completed chunks back here to be
    recorded.

    Raises :class:`IntegrityError` if the result fails its checksum,
    :class:`NotEnoughSpace` if it could not fit, and :class:`Cancelled` if
    stopped. An interrupted download keeps its part file and its recorded
    progress, so the next attempt fetches only what is missing.
    """
    dest = Path(dest_path)
    part = part_path_for(dest)
    ordered = sorted(chunks, key=lambda c: c.sequence)
    total = sum(c.size_bytes for c in ordered) or 1
    key = str(dest)

    offsets: dict[int, int] = {}
    running = 0
    for chunk in ordered:
        offsets[chunk.sequence] = running
        running += chunk.size_bytes

    already: set[int] = set()
    if progress_store is not None:
        progress_store.reset_if_changed(key, file_id, total)
        if part.exists():
            already = progress_store.completed(key, file_id, total)
            if not already:
                already = _adopt_sequential_part(
                    part, ordered, total, progress_store, key, file_id
                )
        else:
            # No output to resume into; whatever was recorded is meaningless.
            progress_store.clear(key)

    remaining = [c for c in ordered if c.sequence not in already]
    done_bytes = sum(c.size_bytes for c in ordered if c.sequence in already)
    check_space(dest, total - done_bytes)

    workers = streams or stream_count()
    if already:
        log.info(
            "resuming %s: %d of %d chunks present (%.2f GB), %d streams",
            dest.name, len(already), len(ordered), done_bytes / 1024**3, workers,
        )
    else:
        log.info(
            "downloading %s: %d chunks, %.2f GB, %d streams",
            dest.name, len(ordered), total / 1024**3, workers,
        )

    if not remaining:
        log.info("%s was already complete; verifying", dest.name)

    # Preallocate so every worker has its region to write into.
    with open(part, "a+b") as fh:
        fh.truncate(total)

    completed_count = len(already)
    try:
        with open(part, "r+b") as out:
            fd = out.fileno()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                queue = iter(remaining)
                in_flight: dict = {}

                def submit_next() -> bool:
                    """Keep at most `workers` chunks in flight.

                    Submitting all of them up front would mean that after one
                    failed, the pool carried on fetching the rest — bytes
                    written but never recorded, so re-fetched on the next
                    attempt. A bounded window stops promptly instead.
                    """
                    chunk = next(queue, None)
                    if chunk is None:
                        return False
                    future = pool.submit(
                        _fetch_chunk,
                        credentials_for(chunk.account_id),
                        chunk,
                        fd,
                        offsets[chunk.sequence],
                        should_stop,
                    )
                    in_flight[future] = chunk
                    return True

                for _ in range(workers):
                    if not submit_next():
                        break

                try:
                    while in_flight:
                        finished, _ = wait(
                            in_flight, return_when=FIRST_COMPLETED, timeout=1.0
                        )
                        for future in finished:
                            chunk = in_flight.pop(future)
                            try:
                                size = future.result()
                            except Exception as exc:
                                log.error(
                                    "chunk %d (account %s) failed: %s: %s",
                                    chunk.sequence, chunk.account_id,
                                    type(exc).__name__, exc,
                                )
                                raise type(exc)(
                                    f"chunk {chunk.sequence + 1} of "
                                    f"{len(ordered)} failed: {exc}"
                                ) from exc
                            if progress_store is not None:
                                progress_store.record(
                                    key, file_id, total, chunk.sequence, size
                                )
                            done_bytes += size
                            completed_count += 1
                            if on_chunk is not None:
                                on_chunk(completed_count, len(ordered))
                            if on_progress is not None:
                                on_progress(min(done_bytes / total, 1.0))
                            submit_next()
                finally:
                    for future in in_flight:
                        future.cancel()

        if expected_md5:
            digest = hashlib.md5()
            with open(part, "rb") as fh:
                for block in iter(lambda: fh.read(4 * 1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected_md5:
                raise IntegrityError(
                    f"Checksum mismatch for {dest.name}: expected {expected_md5}, "
                    f"got {digest.hexdigest()}."
                )

        part.replace(dest)
        if progress_store is not None:
            progress_store.clear(key)
        log.info("downloaded %s (%.2f GB)", dest.name, total / 1024**3)
        if on_progress is not None:
            on_progress(1.0)
        return dest
    except IntegrityError:
        # Wrong bytes, not merely missing ones: resuming onto them would
        # preserve the corruption, so start clean next time.
        log.error("whole-file checksum failed for %s; discarding", dest.name)
        part.unlink(missing_ok=True)
        if progress_store is not None:
            progress_store.clear(key)
        raise
    except Exception:
        log.warning(
            "download of %s interrupted at %.2f GB of %.2f GB; %d chunks kept",
            dest.name, done_bytes / 1024**3, total / 1024**3, completed_count,
        )
        raise
