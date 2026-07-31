"""List an account's app-created Drive files, for index reconciliation.

Under the `drive.file` scope, files.list only returns files this app created,
which is exactly the set the index should mirror. Used by the repair/re-sync
flow to prune index entries whose Drive file is gone and to recover Drive files
missing from the index.
"""

from __future__ import annotations

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from gstore.drive.rate_limit import execute_with_backoff


def list_app_files(credentials: Credentials) -> dict[str, dict]:
    """Return {drive_file_id: {id, name, size, md5Checksum}} for app files."""
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    files: dict[str, dict] = {}
    page_token: str | None = None
    while True:
        response = execute_with_backoff(
            lambda token=page_token: service.files()
            .list(
                q="trashed=false",
                spaces="drive",
                fields="nextPageToken, files(id, name, size, md5Checksum)",
                pageSize=1000,
                pageToken=token,
            )
            .execute()
        )
        for entry in response.get("files", []):
            files[entry["id"]] = entry
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files
