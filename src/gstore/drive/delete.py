"""Delete a file from an account's Drive."""

from __future__ import annotations

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from gstore.drive.rate_limit import execute_with_backoff


def _status_of(err: HttpError) -> int | None:
    status = getattr(err, "status_code", None)
    if status is None and err.resp is not None:
        status = err.resp.status
    return status


def delete_drive_file(credentials: Credentials, drive_file_id: str) -> None:
    """Permanently delete a file. A 404 (already gone) is treated as success.

    Used for Gstore's own files (upload cleanup, index deletes) where a hard,
    quota-reclaiming delete is intended. For files the user merely *viewed* in
    the live scan, prefer :func:`trash_drive_file` (recoverable).
    """
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    try:
        execute_with_backoff(
            lambda: service.files().delete(fileId=drive_file_id).execute()
        )
    except HttpError as err:
        if _status_of(err) == 404:
            return  # nothing to delete
        raise


def trash_drive_file(credentials: Credentials, drive_file_id: str) -> None:
    """Move a file to Trash (recoverable ~30 days). A 404 is treated as success.

    Used by the live-scan delete: acting on the user's whole Drive, a reversible
    trash is far safer than a permanent erase. Requires the full `drive` scope.
    """
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    try:
        execute_with_backoff(
            lambda: service.files()
            .update(fileId=drive_file_id, body={"trashed": True})
            .execute()
        )
    except HttpError as err:
        if _status_of(err) == 404:
            return  # already gone
        raise
