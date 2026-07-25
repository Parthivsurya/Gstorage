"""Stream a Drive file to local disk.

Downloads in chunks straight to the destination file — no full in-memory buffer.
Progress is reported per chunk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from gstore.drive.rate_limit import execute_with_backoff

_CHUNK_BYTES = 5 * 1024 * 1024  # 5 MB


def download_file(
    credentials: Credentials,
    drive_file_id: str,
    dest_path: str | Path,
    on_progress: Callable[[float], None] | None = None,
) -> Path:
    """Download `drive_file_id` to `dest_path`; returns the written path."""
    dest = Path(dest_path)
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    request = service.files().get_media(fileId=drive_file_id)

    with open(dest, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=_CHUNK_BYTES)
        done = False
        while not done:
            status, done = execute_with_backoff(downloader.next_chunk)
            if status is not None and on_progress is not None:
                on_progress(status.progress())

    if on_progress is not None:
        on_progress(1.0)
    return dest
