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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from google.oauth2.credentials import Credentials
from googleapiclient.http import MediaIoBaseUpload

from gstore.drive.delete import delete_drive_file
from gstore.drive.hashing import IntegrityError
from gstore.drive.placement import ChunkPlan
from gstore.drive.rate_limit import Cancelled, execute_with_backoff
from gstore.drive.service import drive_service

_UPLOAD_CHUNK_BYTES = 5 * 1024 * 1024  # resumable transfer granularity per chunk
_HASH_BLOCK = 1024 * 1024  # read size while checksumming a chunk

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


class _FileSlice:
    """A read-only, seekable view of one byte range of an already-open file.

    MediaIoBaseUpload only ever calls seek/tell/read, so a chunk can be fed
    straight from the original file instead of being loaded into memory.
    Reading 50 MB and then handing it to io.BytesIO — which copies — meant
    ~100 MB live per chunk; this holds only the 5 MB piece in flight.

    Positions are relative to the slice, so the stream looks exactly like a
    `length`-byte file. The underlying handle is seeked on every read, so the
    caller must not interleave its own reads on it.
    """

    def __init__(self, fh, offset: int, length: int) -> None:
        self._fh = fh
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
        self._fh.seek(self._offset + self._pos)
        data = self._fh.read(size)
        self._pos += len(data)
        return data


def _slice_md5(fh, offset: int, length: int) -> str:
    """Checksum a byte range in blocks, and confirm it is all actually there.

    Returning early on a short read is the short-file guard: a file truncated
    mid-upload would otherwise have a stunted chunk stored, and nothing would
    notice until reassembly failed at download time.
    """
    digest = hashlib.md5()
    read = 0
    fh.seek(offset)
    while read < length:
        block = fh.read(min(_HASH_BLOCK, length - read))
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
    service,
    credentials: Credentials,
    name: str,
    fh,
    offset: int,
    length: int,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[float], None] | None = None,
) -> tuple[str, str]:
    """Upload one byte range of `fh` as a Drive file; verify + return (id, md5).

    Streams the range straight from the open file — nothing is buffered beyond
    the resumable piece in flight.

    `on_progress` reports this chunk's own 0.0-1.0 completion as each inner
    resumable piece lands, so a caller can show movement inside a 50 MB chunk
    instead of only between chunks.
    """
    local_md5 = _slice_md5(fh, offset, length)
    media = MediaIoBaseUpload(
        _FileSlice(fh, offset, length),
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

    # One Drive service per account, not per chunk. A 20 GB file is 410
    # chunks; building a service (and a fresh TLS connection) for each was
    # pure churn.
    services: dict[int, object] = {}

    def service_for(account_id: int):
        service = services.get(account_id)
        if service is None:
            service = drive_service(credentials_for(account_id))
            services[account_id] = service
        return service

    try:
        with open(path, "rb") as fh:
            for plan in ordered:
                if should_stop is not None and should_stop():
                    raise Cancelled("upload cancelled")

                # Report inside the chunk too. A 20 GB file is 410 chunks, so
                # chunk-granular progress alone leaves the bar motionless for
                # minutes at a time and the upload looks hung.
                base = done_bytes

                def chunk_progress(fraction: float, _base=base, _len=plan.length):
                    if on_progress is not None:
                        on_progress((_base + fraction * _len) / total)

                drive_id, md5 = _upload_range(
                    service_for(plan.account_id),
                    credentials_for(plan.account_id),
                    part_name(path.name, plan.sequence),
                    fh,
                    plan.offset,
                    plan.length,
                    should_stop,
                    chunk_progress,
                )
                result = ChunkUploadResult(
                    sequence=plan.sequence,
                    account_id=plan.account_id,
                    drive_file_id=drive_id,
                    md5=md5,
                    size_bytes=plan.length,
                )
                if resume is not None:
                    resume.record(result)  # staged: survives an interruption
                else:
                    uploaded_this_run.append(result)
                results.append(result)
                done_bytes += plan.length
                if on_progress is not None:
                    on_progress(done_bytes / total)
        return sorted(results, key=lambda r: r.sequence)
    except Exception:
        # Only non-resumable uploads clean up; resumable ones keep staged chunks.
        for result in uploaded_this_run:
            try:
                delete_drive_file(
                    credentials_for(result.account_id), result.drive_file_id
                )
            except Exception:
                pass  # best-effort; the original error is what matters
        raise
