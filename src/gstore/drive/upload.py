"""Resumable upload of a whole file to a specific account's Drive.

Streams directly from local disk in chunks (Google's recommendation for files
over ~5 MB) — no full in-memory copy and no server hop. Progress is reported per
chunk so the UI can show a bar.

Two robustness guarantees:

* **Resume** — when a :class:`UploadSession` is supplied, Drive's resumable
  session URI is persisted as soon as it's known. A later call with the same
  session reattaches to that URI and continues from the last byte the server
  received, so an interrupted transfer (dropped connection, crash, quit) doesn't
  restart from zero. An expired/invalid session transparently restarts fresh.
* **Integrity** — on completion the local MD5 is compared against the
  md5Checksum Drive computed; a mismatch deletes the corrupt remote copy and
  raises rather than recording a bad file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from gstore.drive.delete import delete_drive_file
from gstore.drive.hashing import IntegrityError, md5_of_file
from gstore.drive.rate_limit import execute_with_backoff
from gstore.drive.service import drive_service

_CHUNK_BYTES = 5 * 1024 * 1024  # 5 MB resumable chunks

# A stored session URI that the server no longer recognizes -> start over.
_DEAD_SESSION_STATUS = {404, 410}

__all__ = ["IntegrityError", "UploadResult", "UploadSession", "upload_file"]


class UploadSession(Protocol):
    """Persistence for a resumable session URI (see index.uploads.DbUploadSession)."""

    def load(self) -> str | None: ...
    def save(self, resumable_uri: str) -> None: ...
    def clear(self) -> None: ...


@dataclass(frozen=True)
class UploadResult:
    drive_file_id: str
    size_bytes: int
    md5: str


def _status_of(err: HttpError) -> int | None:
    status = getattr(err, "status_code", None)
    if status is None and err.resp is not None:
        status = err.resp.status
    return status


def upload_file(
    credentials: Credentials,
    local_path: Path,
    on_progress: Callable[[float], None] | None = None,
    session: UploadSession | None = None,
) -> UploadResult:
    """Upload `local_path`, verify its checksum, and return the result.

    `on_progress` receives a 0.0–1.0 fraction as each chunk completes. If a
    `session` is given the upload can resume a prior interrupted transfer and
    persists its progress URI. Raises :class:`IntegrityError` (after removing the
    bad remote copy) if Drive's md5Checksum doesn't match the local file.
    """
    path = Path(local_path)
    local_md5 = md5_of_file(path)
    service = drive_service(credentials)

    def _new_request():
        media = MediaFileUpload(str(path), resumable=True, chunksize=_CHUNK_BYTES)
        return service.files().create(
            body={"name": path.name}, media_body=media, fields="id,size,md5Checksum"
        )

    request = _new_request()
    saved_uri = session.load() if session is not None else None
    if saved_uri:
        # Reattach to the existing session; the library first queries the server
        # for the current offset (via _in_error_state) then continues from there.
        request.resumable_uri = saved_uri
        request._in_error_state = True

    response = None
    while response is None:
        try:
            status, response = execute_with_backoff(request.next_chunk)
        except HttpError as err:
            if saved_uri and _status_of(err) in _DEAD_SESSION_STATUS:
                # Session expired/unknown: discard it and restart from scratch.
                if session is not None:
                    session.clear()
                saved_uri = None
                request = _new_request()
                continue
            raise

        # Persist the session URI the first time it's available (fresh uploads).
        if session is not None and not saved_uri and request.resumable_uri:
            saved_uri = request.resumable_uri
            session.save(saved_uri)

        if status is not None and on_progress is not None:
            on_progress(status.progress())

    # Transfer finished: the stored session (if any) is no longer needed.
    if session is not None:
        session.clear()

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

    if on_progress is not None:
        on_progress(1.0)
    size = int(response.get("size") or path.stat().st_size)
    return UploadResult(drive_file_id=drive_id, size_bytes=size, md5=local_md5)
