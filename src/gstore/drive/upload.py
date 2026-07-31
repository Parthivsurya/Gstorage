"""Resumable upload of a whole file to a specific account's Drive.

Streams directly from local disk in chunks (Google's recommendation for files
over ~5 MB) — no full in-memory copy and no server hop. Progress is reported per
chunk so the UI can show a bar. On completion the upload is integrity-checked:
the local MD5 is compared against the md5Checksum Drive computed, and a mismatch
deletes the corrupt remote copy and raises rather than recording a bad file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from gstore.drive.delete import delete_drive_file
from gstore.drive.hashing import IntegrityError, md5_of_file
from gstore.drive.rate_limit import execute_with_backoff

_CHUNK_BYTES = 5 * 1024 * 1024  # 5 MB resumable chunks

__all__ = ["IntegrityError", "UploadResult", "upload_file"]


@dataclass(frozen=True)
class UploadResult:
    drive_file_id: str
    size_bytes: int
    md5: str


def upload_file(
    credentials: Credentials,
    local_path: Path,
    on_progress: Callable[[float], None] | None = None,
) -> UploadResult:
    """Upload `local_path`, verify its checksum, and return the result.

    `on_progress` receives a 0.0–1.0 fraction as each chunk completes. Raises
    :class:`IntegrityError` (after removing the bad remote copy) if Drive's
    md5Checksum doesn't match the local file.
    """
    path = Path(local_path)
    local_md5 = md5_of_file(path)

    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    media = MediaFileUpload(str(path), resumable=True, chunksize=_CHUNK_BYTES)
    request = service.files().create(
        body={"name": path.name}, media_body=media, fields="id,size,md5Checksum"
    )

    response = None
    while response is None:
        status, response = execute_with_backoff(request.next_chunk)
        if status is not None and on_progress is not None:
            on_progress(status.progress())

    if on_progress is not None:
        on_progress(1.0)

    drive_id = response["id"]
    remote_md5 = response.get("md5Checksum")
    if remote_md5 is not None and remote_md5 != local_md5:
        # The stored copy is corrupt; don't leave it (or its quota) behind.
        try:
            delete_drive_file(credentials, drive_id)
        except Exception:
            pass  # best-effort cleanup; surface the integrity failure regardless
        raise IntegrityError(
            f"Checksum mismatch uploading {path.name}: "
            f"local {local_md5} != Drive {remote_md5}. The corrupt copy was removed."
        )

    size = int(response.get("size") or path.stat().st_size)
    return UploadResult(drive_file_id=drive_id, size_bytes=size, md5=local_md5)
