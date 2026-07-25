"""Delete a file from an account's Drive."""

from __future__ import annotations

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gstore.drive.rate_limit import execute_with_backoff


def delete_drive_file(credentials: Credentials, drive_file_id: str) -> None:
    """Permanently delete a file. A 404 (already gone) is treated as success."""
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    try:
        execute_with_backoff(
            lambda: service.files().delete(fileId=drive_file_id).execute()
        )
    except HttpError as err:
        status = getattr(err, "status_code", None)
        if status is None and err.resp is not None:
            status = err.resp.status
        if status == 404:
            return  # nothing to delete
        raise
