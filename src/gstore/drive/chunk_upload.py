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
from typing import Callable

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

from gstore.drive.delete import delete_drive_file
from gstore.drive.hashing import IntegrityError
from gstore.drive.placement import ChunkPlan
from gstore.drive.rate_limit import execute_with_backoff

_UPLOAD_CHUNK_BYTES = 5 * 1024 * 1024  # resumable transfer granularity per chunk

# Provides credentials for a given account id (cached by the caller).
CredentialsFor = Callable[[int], Credentials]


@dataclass(frozen=True)
class ChunkUploadResult:
    sequence: int
    account_id: int
    drive_file_id: str
    md5: str
    size_bytes: int


def _upload_bytes(credentials: Credentials, name: str, data: bytes) -> tuple[str, str]:
    """Upload a bytes payload as one Drive file; verify + return (id, md5)."""
    local_md5 = hashlib.md5(data).hexdigest()
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
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
        _status, response = execute_with_backoff(request.next_chunk)

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
) -> list[ChunkUploadResult]:
    """Upload every chunk in `assignments`; return their results in order.

    On any failure, deletes the chunks already uploaded (best effort) and
    re-raises, so no partial remnants remain.
    """
    path = Path(local_path)
    ordered = sorted(assignments, key=lambda p: p.sequence)
    total = sum(p.length for p in ordered) or 1
    done_bytes = 0
    uploaded: list[ChunkUploadResult] = []

    try:
        with open(path, "rb") as fh:
            for plan in ordered:
                fh.seek(plan.offset)
                data = fh.read(plan.length)
                name = f"{path.name}.gstore-part-{plan.sequence:05d}"
                drive_id, md5 = _upload_bytes(
                    credentials_for(plan.account_id), name, data
                )
                uploaded.append(
                    ChunkUploadResult(
                        sequence=plan.sequence,
                        account_id=plan.account_id,
                        drive_file_id=drive_id,
                        md5=md5,
                        size_bytes=len(data),
                    )
                )
                done_bytes += len(data)
                if on_progress is not None:
                    on_progress(done_bytes / total)
        return uploaded
    except Exception:
        for result in uploaded:
            try:
                delete_drive_file(
                    credentials_for(result.account_id), result.drive_file_id
                )
            except Exception:
                pass  # best-effort; the original error is what matters
        raise
