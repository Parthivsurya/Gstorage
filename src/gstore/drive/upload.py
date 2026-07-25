"""Resumable upload of a whole file to a specific account's Drive.

Streams directly from local disk in chunks (Google's recommendation for files
over ~5 MB) — no full in-memory copy and no server hop. Progress is reported per
chunk so the UI can show a bar.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from gstore.drive.rate_limit import execute_with_backoff

_CHUNK_BYTES = 5 * 1024 * 1024  # 5 MB resumable chunks


def upload_file(
    credentials: Credentials,
    local_path: Path,
    on_progress: Callable[[float], None] | None = None,
) -> tuple[str, int]:
    """Upload `local_path` and return (drive_file_id, size_bytes).

    `on_progress` receives a 0.0–1.0 fraction as each chunk completes.
    """
    path = Path(local_path)
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    media = MediaFileUpload(str(path), resumable=True, chunksize=_CHUNK_BYTES)
    request = service.files().create(
        body={"name": path.name}, media_body=media, fields="id,size"
    )

    response = None
    while response is None:
        status, response = execute_with_backoff(request.next_chunk)
        if status is not None and on_progress is not None:
            on_progress(status.progress())

    if on_progress is not None:
        on_progress(1.0)

    size = int(response.get("size") or path.stat().st_size)
    return response["id"], size
