"""The per-account folder that everything Gstore uploads is placed inside.

``files.create`` with no ``parents`` drops a file into My Drive's root, so a
single chunked upload used to scatter hundreds of 50 MB pieces across the top
level of the user's own Drive. Everything the app writes goes inside one folder
per account instead.

The folder is held by **Drive id, never by name or path.** The user may rename
it, drag it somewhere else, or already own an unrelated folder called "Gstore" —
none of that matters and none of it is looked up by name. It also means no new
OAuth scope is involved: a folder is a file, so ``drive.file`` covers creating
one and using what we created as a parent.

Tidiness must never cost a transfer. Every entry point here degrades to
``None`` — upload to the root, exactly as before — rather than raising, so a
folder that cannot be created or confirmed costs nothing and breaks nothing.
"""

from __future__ import annotations

from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

from gstore.applog import get as get_logger
from gstore.drive.rate_limit import execute_with_backoff
from gstore.drive.service import drive_service

FOLDER_MIME = "application/vnd.google-apps.folder"
FOLDER_NAME = "Gstore"

# What a stored folder id turned out to be worth.
OK = "ok"
TRASHED = "trashed"
MISSING = "missing"

log = get_logger(__name__)

__all__ = [
    "FOLDER_MIME",
    "FOLDER_NAME",
    "FolderResolver",
    "create_folder",
    "ensure_folder",
    "folder_status",
    "restore_folder",
]


def _status_of(err: HttpError) -> int | None:
    status = getattr(err, "status_code", None)
    if status is None and err.resp is not None:
        status = err.resp.status
    return status


def create_folder(credentials: Credentials, name: str = FOLDER_NAME) -> str:
    """Create the folder on this account and return its Drive id."""
    service = drive_service(credentials)
    request = service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME}, fields="id"
    )
    return execute_with_backoff(request.execute)["id"]


def folder_status(credentials: Credentials, folder_id: str) -> str:
    """Whether `folder_id` is usable, sitting in the trash, or gone for good."""
    service = drive_service(credentials)
    request = service.files().get(fileId=folder_id, fields="id,trashed")
    try:
        found = execute_with_backoff(request.execute)
    except HttpError as err:
        if _status_of(err) == 404:
            return MISSING
        raise
    return TRASHED if found.get("trashed") else OK


def restore_folder(credentials: Credentials, folder_id: str) -> None:
    """Take the folder back out of the trash, contents and all.

    A trashed folder is restored, never replaced. Creating a replacement would
    leave every file already inside it sitting in the user's trash, orphaned
    from an index that still points at them — which looks exactly like total
    data loss while nothing has actually been lost.
    """
    service = drive_service(credentials)
    request = service.files().update(fileId=folder_id, body={"trashed": False})
    execute_with_backoff(request.execute)


def ensure_folder(credentials: Credentials, folder_id: str | None) -> str | None:
    """Return a folder id that is safe to upload into, or None.

    `folder_id` is whatever the index remembers for this account, if anything.
    A `None` return means the caller should upload to the root as it always
    did: this is housekeeping and it is never worth failing a transfer over.
    """
    try:
        if folder_id:
            state = folder_status(credentials, folder_id)
            if state == OK:
                return folder_id
            if state == TRASHED:
                restore_folder(credentials, folder_id)
                log.info("restored the Gstore folder from the trash")
                return folder_id
            log.info("the Gstore folder was deleted for good; creating another")
        return create_folder(credentials)
    except Exception as exc:  # noqa: BLE001 — see the module docstring
        log.warning("no Gstore folder this time, uploading to the root: %s", exc)
        return None


class FolderResolver:
    """Each account's folder id, confirmed once per process and then reused.

    The first upload touching an account costs one ``files.get`` (or a
    ``files.create``, once ever); every upload after that costs nothing. That
    matters: a 400-piece chunked upload spread over three accounts pays three
    round trips here rather than four hundred.

    Resolution reads and writes the SQLite index, so — exactly like the
    credentials in :func:`~gstore.drive.chunk_upload.upload_chunks` — it has to
    happen on the thread that owns the connection, before any upload worker
    starts. A connection belongs to the thread that opened it.
    """

    def __init__(self, accounts) -> None:
        self._accounts = accounts
        self._cache: dict[int, str | None] = {}

    def folder_id(self, account_id: int) -> str | None:
        """The folder to upload this account's files into, or None for the root."""
        if account_id in self._cache:
            return self._cache[account_id]

        resolved: str | None = None
        try:
            stored = self._accounts.drive_folder_id(account_id)
            resolved = ensure_folder(
                self._accounts.get_credentials(account_id), stored
            )
            if resolved and resolved != stored:
                self._accounts.set_drive_folder_id(account_id, resolved)
        except Exception as exc:  # noqa: BLE001 — never block an upload
            log.warning("no Gstore folder for account %s: %s", account_id, exc)

        self._cache[account_id] = resolved
        return resolved
