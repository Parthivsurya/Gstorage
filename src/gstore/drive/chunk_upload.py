"""Upload a large file as chunks distributed across accounts (Phase B).

Each chunk is a byte range of the local file uploaded as its own Drive file on
its assigned account, verified against Drive's md5Checksum. If any chunk fails,
the chunks already uploaded are deleted so a partial upload never leaks orphaned
Drive files (and their quota). The caller records the successful chunks in the
index; nothing is persisted here.
"""

from __future__ import annotations

import hashlib
import io
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


def _upload_bytes(
    credentials: Credentials,
    name: str,
    data: bytes,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[str, str]:
    """Upload a bytes payload as one Drive file; verify + return (id, md5)."""
    local_md5 = hashlib.md5(data).hexdigest()
    service = drive_service(credentials)
    media = MediaIoBaseUpload(
        io.BytesIO(data),
        mimetype="application/octet-stream",
        resumable=True,
        chunksize=_UPLOAD_CHUNK_BYTES,
    )
    request = service.files().create(
        body={"name": name}, media_body=media, fields="id,md5Checksum"
    )
    response = None
    while response is None:
        _status, response = execute_with_backoff(
            request.next_chunk, should_stop=should_stop
        )

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

    try:
        with open(path, "rb") as fh:
            for plan in ordered:
                if should_stop is not None and should_stop():
                    raise Cancelled("upload cancelled")
                fh.seek(plan.offset)
                data = fh.read(plan.length)
                drive_id, md5 = _upload_bytes(
                    credentials_for(plan.account_id),
                    part_name(path.name, plan.sequence),
                    data,
                    should_stop,
                )
                result = ChunkUploadResult(
                    sequence=plan.sequence,
                    account_id=plan.account_id,
                    drive_file_id=drive_id,
                    md5=md5,
                    size_bytes=len(data),
                )
                if resume is not None:
                    resume.record(result)  # staged: survives an interruption
                else:
                    uploaded_this_run.append(result)
                results.append(result)
                done_bytes += len(data)
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
