"""Live listing of everything in an account's Drive (read-only).

Powers the combined "Scan Drive now" view: unlike :mod:`gstore.drive.reconcile`
(which lists only app-created files under `drive.file`), this walks *all* files
visible under the `drive.readonly` scope. It reads — it never mutates Drive.

An account still linked with only the older `drive.file` scope will silently see
just its Gstore files here; :data:`DRIVE_READONLY_SCOPE` lets callers detect that
and prompt a re-link.
"""

from __future__ import annotations

from dataclasses import dataclass

from google.oauth2.credentials import Credentials

from gstore.auth.oauth import DRIVE_FULL_SCOPE, DRIVE_READONLY_SCOPE
from gstore.drive.rate_limit import execute_with_backoff
from gstore.drive.service import drive_service

_FOLDER_MIME = "application/vnd.google-apps.folder"
_GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."


@dataclass(frozen=True)
class DriveFile:
    """One entry in an account's Drive, as seen by a live scan."""

    id: str
    name: str
    size: int | None  # None for folders and Google-native docs (no byte size)
    mime_type: str
    modified_time: str | None
    md5: str | None
    # Short-lived, auth-required URL to a Drive-rendered preview (images, PDFs,
    # docs). None when Drive has no thumbnail. Used by the Gallery view.
    thumbnail_link: str | None = None

    @property
    def is_folder(self) -> bool:
        return self.mime_type == _FOLDER_MIME

    @property
    def is_google_doc(self) -> bool:
        """Google Docs/Sheets/Slides etc. — no direct bytes; export-only."""
        return (
            self.mime_type.startswith(_GOOGLE_NATIVE_PREFIX) and not self.is_folder
        )

    @property
    def is_downloadable(self) -> bool:
        """True for regular binary files we can stream via get_media."""
        return not self.is_folder and not self.is_google_doc


def _to_drive_file(entry: dict) -> DriveFile:
    """Map one Drive API `files.list` record to a :class:`DriveFile`."""
    raw_size = entry.get("size")
    return DriveFile(
        id=entry["id"],
        name=entry.get("name", "untitled"),
        size=int(raw_size) if raw_size is not None else None,
        mime_type=entry.get("mimeType", ""),
        modified_time=entry.get("modifiedTime"),
        md5=entry.get("md5Checksum"),
        thumbnail_link=entry.get("thumbnailLink"),
    )


def has_full_read(credentials: Credentials) -> bool:
    """Whether these credentials can see the whole Drive (not just app files).

    True for either the read-only or the full `drive` scope — both list
    everything.
    """
    scopes = getattr(credentials, "scopes", None) or []
    return DRIVE_READONLY_SCOPE in scopes or DRIVE_FULL_SCOPE in scopes


def can_modify(credentials: Credentials) -> bool:
    """Whether these credentials may modify/trash arbitrary Drive files.

    Requires the full `drive` scope; read-only or `drive.file` tokens cannot
    trash files the app didn't create.
    """
    scopes = getattr(credentials, "scopes", None) or []
    return DRIVE_FULL_SCOPE in scopes


def list_all_files(credentials: Credentials) -> list[DriveFile]:
    """Every non-trashed file/folder visible to `credentials`, paginated.

    Folders sort before files, then by name, matching how Drive itself lists a
    directory. Under `drive.readonly` this is the account's full Drive; under
    `drive.file` it collapses to just the app's own files.
    """
    service = drive_service(credentials)
    results: list[DriveFile] = []
    page_token: str | None = None
    while True:
        response = execute_with_backoff(
            lambda token=page_token: service.files()
            .list(
                q="trashed = false",
                spaces="drive",
                fields=(
                    "nextPageToken, files(id, name, size, mimeType, "
                    "modifiedTime, md5Checksum, thumbnailLink)"
                ),
                orderBy="folder,name_natural",
                pageSize=1000,
                pageToken=token,
            )
            .execute()
        )
        results.extend(_to_drive_file(e) for e in response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return results
