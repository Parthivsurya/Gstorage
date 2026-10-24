"""Upload a large file as chunks distributed across accounts (Phase B).

Each chunk is a byte range of the local file uploaded as its own Drive file on
its assigned account, verified against Drive's md5Checksum. Ranges are streamed
straight from the open file, so memory stays at the size of the resumable piece
in flight rather than the whole chunk. If any chunk fails,
the chunks already uploaded are deleted so a partial upload never leaks orphaned
Drive files (and their quota). The caller records the successful chunks in the
index; nothing is persisted here.
"""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Lock
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from google.oauth2.credentials import Credentials
from googleapiclient.http import MediaIoBaseUpload

from gstore.applog import get as get_logger
from gstore.drive.delete import delete_drive_file
from gstore.drive.hashing import IntegrityError
from gstore.drive.placement import ChunkPlan
from gstore.drive.rate_limit import Cancelled, execute_with_backoff
from gstore.drive.service import drive_service

_UPLOAD_CHUNK_BYTES = 5 * 1024 * 1024  # resumable transfer granularity per chunk
_HASH_BLOCK = 1024 * 1024  # read size while checksumming a chunk

log = get_logger(__name__)

# How many chunks to send at once. A single stream is held well below the
# link's capacity by latency and per-connection limits — on a 5G line rated
# 80 Mb/s, sequential chunks managed 6.9 Mbps while eight parallel download
# streams reached 30. Four is deliberately short of what the link can take,
# since every stream spends Drive API quota and shares a per-user rate limit.
DEFAULT_STREAMS = 4
_STREAMS_ENV = "GSTORE_UPLOAD_STREAMS"
_MAX_STREAMS = 16


def stream_count() -> int:
    """Concurrent chunk uploads, from the environment or the default."""
    raw = os.environ.get(_STREAMS_ENV)
    if not raw:
        return DEFAULT_STREAMS
    try:
        return max(1, min(int(raw), _MAX_STREAMS))
    except ValueError:
        log.warning("%s=%r is not a number; using %d", _STREAMS_ENV, raw, DEFAULT_STREAMS)
        return DEFAULT_STREAMS

# Marker in a chunk part-file's Drive name; reconcile uses it to spot orphans.
PART_MARKER = ".gstore-part-"

# Provides credentials for a given account id (cached by the caller).
CredentialsFor = Callable[[int], Credentials]


def part_name(base_name: str, sequence: int) -> str:
    """Drive file name for one chunk of `base_name`."""
    return f"{base_name}{PART_MARKER}{sequence:05d}"


@dataclass(frozen=True)
class ChunkUploadResult:
    sequence: int
    account_id: int
    drive_file_id: str
    md5: str
    size_bytes: int


class ChunkResume(Protocol):
    """Records completed chunks so an interrupted upload can resume (worker-backed)."""

    def completed(self) -> dict[int, ChunkUploadResult]: ...
    def record(self, result: ChunkUploadResult) -> None: ...


class _FdSlice:
    """A read-only, seekable view of one byte range of an open file.

    MediaIoBaseUpload only ever calls seek/tell/read, so a chunk is fed
    straight from the original file rather than loaded into memory — reading
    50 MB and handing it to io.BytesIO, which copies, meant ~100 MB live per
    chunk; this holds only the piece in flight.

    Reads go through ``os.pread``, which is positional and leaves the
    descriptor's shared offset alone. That is what lets several workers read
    different ranges of the same file at once: a seek-then-read pair would
    race, with two threads silently reading each other's bytes.
    """

    def __init__(self, fd: int, offset: int, length: int) -> None:
        self._fd = fd
        self._offset = offset
        self._length = length
        self._pos = 0

    def seek(self, pos: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            target = pos
        elif whence == os.SEEK_CUR:
            target = self._pos + pos
        elif whence == os.SEEK_END:
            target = self._length + pos
        else:
            raise ValueError(f"invalid whence: {whence}")
        self._pos = max(0, min(target, self._length))
        return self._pos

    def tell(self) -> int:
        return self._pos

    def read(self, size: int = -1) -> bytes:
        remaining = self._length - self._pos
        if size is None or size < 0:
            size = remaining
        size = min(size, remaining)
        if size <= 0:
            return b""
        data = os.pread(self._fd, size, self._offset + self._pos)
        self._pos += len(data)
        return data


def _slice_md5(fd: int, offset: int, length: int) -> str:
    """Checksum a byte range in blocks, and confirm it is all actually there.

    Returning early on a short read is the short-file guard: a file truncated
    mid-upload would otherwise have a stunted chunk stored, and nothing would
    notice until reassembly failed at download time.
    """
    digest = hashlib.md5()
    read = 0
    while read < length:
        block = os.pread(fd, min(_HASH_BLOCK, length - read), offset + read)
        if not block:
            break
        digest.update(block)
        read += len(block)
    if read != length:
        raise IntegrityError(
            f"file changed while uploading: expected {length} bytes at offset "
            f"{offset}, got {read}."
        )
    return digest.hexdigest()


def _upload_range(
    credentials: Credentials,
    name: str,
    fd: int,
    offset: int,
    length: int,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> tuple[str, str]:
    """Upload one byte range of `fd` as a Drive file; verify + return (id, md5).

    Streams the range straight from the open file — nothing is buffered beyond
    the resumable piece in flight.

    The Drive service is built here, per call, rather than shared: a
    googleapiclient service wraps one http object and is not safe to use from
    several threads at once.

    `on_progress` reports this chunk's own 0.0-1.0 completion as each inner
    resumable piece lands, so a caller can show movement inside a 50 MB chunk
    instead of only between chunks.
    """
    local_md5 = _slice_md5(fd, offset, length)
    service = drive_service(credentials)
    media = MediaIoBaseUpload(
        _FdSlice(fd, offset, length),
        mimetype="application/octet-stream",
        resumable=True,
        chunksize=_UPLOAD_CHUNK_BYTES,
    )
    request = service.files().create(
        body={"name": name}, media_body=media, fields="id,md5Checksum"
    )
    response = None
    while response is None:
        status, response = execute_with_backoff(
            request.next_chunk, should_stop=should_stop
        )
        if status is not None and on_progress is not None:
            on_progress(status.progress())

    drive_id = response["id"]
    remote_md5 = response.get("md5Checksum")
    if remote_md5 is not None and remote_md5 != local_md5:
        try:
            delete_drive_file(credentials, drive_id)
        except Exception:
            pass
        raise IntegrityError(
            f"Checksum mismatch uploading chunk {name}: "
            f"local {local_md5} != Drive {remote_md5}."
        )
    return drive_id, local_md5


def upload_chunks(
    local_path: str | Path,
    assignments: list[ChunkPlan],
    credentials_for: CredentialsFor,
    on_progress: Callable[[float], None] | None = None,
    resume: ChunkResume | None = None,
    should_stop: Callable[[], bool] | None = None,
    streams: int | None = None,
) -> list[ChunkUploadResult]:
    """Upload the chunks in `assignments`; return all chunk results in order.

    `assignments` holds only the chunks still to upload — already-completed ones
    come from `resume` (when given). Each completed chunk is recorded via
    `resume` so an interruption resumes from the next missing chunk.

    Cleanup on failure depends on resumability: with a `resume` store the
    already-uploaded chunks are kept (staged for the retry); without one they
    are deleted so no orphans remain.

    `should_stop` is polled between chunks and inside each chunk's retry loop.
    With a `resume` store, stopping is therefore a *pause*: the chunks already
    uploaded stay staged and the next attempt continues from the first one
    missing. It raises :class:`Cancelled`.
    """
    path = Path(local_path)
    ordered = sorted(assignments, key=lambda p: p.sequence)
    already = resume.completed() if resume is not None else {}

    total = (
        sum(p.length for p in ordered) + sum(r.size_bytes for r in already.values())
    ) or 1
    done_bytes = sum(r.size_bytes for r in already.values())
    results: list[ChunkUploadResult] = list(already.values())
    uploaded_this_run: list[ChunkUploadResult] = []

    # Resolve credentials before any worker starts. The resume store is backed
    # by SQLite and credentials are read through it, and a connection belongs
    # to the thread that opened it — a worker reaching back raises
    # ProgrammingError.
    creds: dict[int, Credentials] = {
        account_id: credentials_for(account_id)
        for account_id in {p.account_id for p in ordered}
    }

    workers = streams or stream_count()
    log.info(
        "uploading %s: %d chunks to send, %d already staged, %d streams",
        path.name, len(ordered), len(already), workers,
    )

    # Bytes sent for chunks still in flight, so the bar moves inside a 50 MB
    # chunk rather than only between them. Workers write their own key; the
    # reader sums a snapshot, so a lock is enough.
    partial: dict[int, int] = {}
    partial_lock = Lock()

    def report() -> None:
        if on_progress is None:
            return
        with partial_lock:
            inflight = sum(partial.values())
        on_progress(min((done_bytes + inflight) / total, 1.0))

    fd = os.open(path, os.O_RDONLY)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            queue = iter(ordered)
            in_flight: dict = {}

            def submit_next() -> bool:
                """Keep at most `workers` chunks in flight.

                Submitting everything up front would mean that after one
                failed the pool carried on uploading the rest, leaving Drive
                files that were never recorded — orphans the caller cannot
                clean up because it never learned their ids.
                """
                plan = next(queue, None)
                if plan is None:
                    return False
                def track(fraction: float, _seq=plan.sequence, _len=plan.length):
                    with partial_lock:
                        partial[_seq] = int(fraction * _len)
                    # Report straight away rather than waiting for the poll:
                    # on_progress here is a Qt signal emit, which is safe to
                    # call from a worker thread.
                    report()

                future = pool.submit(
                    _upload_range,
                    creds[plan.account_id],
                    part_name(path.name, plan.sequence),
                    fd,
                    plan.offset,
                    plan.length,
                    should_stop,
                    track,
                )
                in_flight[future] = plan
                return True

            for _ in range(workers):
                if not submit_next():
                    break

            try:
                while in_flight:
                    if should_stop is not None and should_stop():
                        raise Cancelled("upload cancelled")
                    finished, _ = wait(
                        in_flight, return_when=FIRST_COMPLETED, timeout=1.0
                    )
                    if not finished:
                        report()  # in-flight chunks still moving
                        continue
                    for future in finished:
                        plan = in_flight.pop(future)
                        try:
                            drive_id, md5 = future.result()
                        except Exception as exc:
                            log.error(
                                "chunk %d (account %s) failed: %s: %s",
                                plan.sequence, plan.account_id,
                                type(exc).__name__, exc,
                            )
                            raise
                        result = ChunkUploadResult(
                            sequence=plan.sequence,
                            account_id=plan.account_id,
                            drive_file_id=drive_id,
                            md5=md5,
                            size_bytes=plan.length,
                        )
                        if resume is not None:
                            # Recorded here, on the calling thread: the store
                            # is SQLite-backed.
                            resume.record(result)
                        else:
                            uploaded_this_run.append(result)
                        results.append(result)
                        done_bytes += plan.length
                        with partial_lock:
                            partial.pop(plan.sequence, None)
                        report()
                        submit_next()
            finally:
                for future in in_flight:
                    future.cancel()

        return sorted(results, key=lambda r: r.sequence)
    except Exception:
        # Only non-resumable uploads clean up; resumable ones keep staged chunks.
        for result in uploaded_this_run:
            try:
                delete_drive_file(creds[result.account_id], result.drive_file_id)
            except Exception:
                pass  # best-effort; the original error is what matters
        log.warning(
            "upload of %s interrupted with %d of %d chunks done",
            path.name, len(results), len(ordered) + len(already),
        )
        raise
    finally:
        os.close(fd)
