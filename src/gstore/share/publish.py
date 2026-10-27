"""Grant and revoke the public permission, and place the manifest.

Sharing a file means marking every one of its pieces readable by anyone
holding the piece's Drive id, then writing the list of those ids somewhere a
recipient can reach. Both steps are ordinary ``drive.file`` work: the pieces
are files this app created, and so is the manifest.

Pieces of one file routinely live on different accounts, so every call here
takes a ``credentials_for`` lookup rather than one set of credentials, the
same shape :mod:`gstore.drive.chunk_download` uses.

Permission changes are one request per piece — 409 of them for a 20 GB file —
so they run on a small pool. The pool is deliberately modest: these are
cheap, fast calls, and the per-user rate limit is the binding constraint, not
bandwidth.
"""

from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from gstore.drive.rate_limit import execute_with_backoff
from gstore.drive.service import drive_service
from gstore.share.manifest import Manifest, Part, encode_link, new_key

log = logging.getLogger(__name__)

CredentialsFor = Callable[[int], Credentials]

# One request per piece, so concurrency helps; but these are small calls
# against a per-user rate limit, and going wide only buys 429s.
PERMISSION_WORKERS = 8

MANIFEST_MIME = "application/octet-stream"
MANIFEST_NAME = "gstore-share.bin"

_ANYONE = {"role": "reader", "type": "anyone"}


class NotShareable(Exception):
    """The file cannot be shared as it stands."""


@dataclass(frozen=True)
class ShareResult:
    link: str
    manifest_file_id: str
    manifest_account_id: int
    part_count: int


def grant_public(credentials: Credentials, drive_file_id: str) -> None:
    """Make one Drive file readable by anyone holding its id."""
    service = drive_service(credentials)
    request = service.permissions().create(
        fileId=drive_file_id, body=dict(_ANYONE), fields="id"
    )
    execute_with_backoff(request.execute)


def revoke_public(credentials: Credentials, drive_file_id: str) -> bool:
    """Remove the anyone-with-link permission. True if one was there.

    Idempotent on purpose: un-sharing is a cleanup path that runs over
    hundreds of pieces, and a piece already private — or already deleted —
    must not stop the rest.
    """
    service = drive_service(credentials)
    try:
        listing = execute_with_backoff(
            service.permissions().list(fileId=drive_file_id, fields="permissions(id,type)").execute
        )
    except HttpError as exc:
        if exc.resp.status == 404:
            return False
        raise

    removed = False
    for permission in listing.get("permissions", []):
        if permission.get("type") != "anyone":
            continue
        try:
            execute_with_backoff(
                service.permissions()
                .delete(fileId=drive_file_id, permissionId=permission["id"])
                .execute
            )
            removed = True
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
    return removed


def _apply_to_every_piece(
    pieces: list[tuple[int, str]],
    credentials_for: CredentialsFor,
    action: Callable[[Credentials, str], object],
    *,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Run `action` over (account_id, drive_file_id) pairs, in parallel."""
    total, done = len(pieces), 0
    if not total:
        return

    with ThreadPoolExecutor(max_workers=min(PERMISSION_WORKERS, total)) as pool:
        futures = {
            pool.submit(action, credentials_for(account_id), drive_id): drive_id
            for account_id, drive_id in pieces
        }
        for future in as_completed(futures):
            if should_stop is not None and should_stop():
                for pending in futures:
                    pending.cancel()
                raise Cancelled("sharing was cancelled")
            future.result()                 # re-raise in the caller's thread
            done += 1
            if on_progress is not None:
                on_progress(done, total)


class Cancelled(Exception):
    """The user stopped a share or un-share partway."""


def _pieces_of(index, file_row) -> list[tuple[int, str]]:
    """(account_id, drive_file_id) for every piece, in sequence order."""
    if file_row.is_chunked:
        chunks = index.list_chunks(file_row.id)
        missing = [c.sequence for c in chunks if not c.drive_file_id or c.account_id is None]
        if missing:
            raise NotShareable(
                f"{len(missing)} of this file's {len(chunks)} pieces are not "
                "available — re-link the account that holds them, or run "
                "Repair / re-sync, and try again"
            )
        return [(c.account_id, c.drive_file_id) for c in chunks]

    if not file_row.drive_file_id or file_row.home_account_id is None:
        raise NotShareable("this file has not finished uploading yet")
    return [(file_row.home_account_id, file_row.drive_file_id)]


def _parts_of(index, file_row) -> tuple[Part, ...]:
    if file_row.is_chunked:
        return tuple(
            Part(drive_file_id=c.drive_file_id, md5=c.md5 or "", size_bytes=c.size_bytes)
            for c in index.list_chunks(file_row.id)
        )
    return (
        Part(
            drive_file_id=file_row.drive_file_id,
            md5=file_row.md5 or "",
            size_bytes=file_row.size_bytes,
        ),
    )


def upload_manifest(credentials: Credentials, sealed: bytes) -> str:
    """Put the sealed manifest on Drive and return its id.

    Deliberately anonymous: a fixed filename and an opaque mime type, so the
    file reveals nothing about what it describes to anyone browsing the Drive
    it sits in. What it contains is encrypted anyway.
    """
    service = drive_service(credentials)
    media = MediaIoBaseUpload(
        io.BytesIO(sealed), mimetype=MANIFEST_MIME, resumable=False
    )
    request = service.files().create(
        body={"name": MANIFEST_NAME, "mimeType": MANIFEST_MIME},
        media_body=media,
        fields="id",
    )
    created = execute_with_backoff(request.execute)
    return created["id"]


def share_file(
    index,
    file_row,
    credentials_for: CredentialsFor,
    *,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> ShareResult:
    """Make a stored file readable by anyone with the returned link."""
    pieces = _pieces_of(index, file_row)
    parts = _parts_of(index, file_row)

    log.info("sharing %r as %d piece(s)", file_row.name, len(pieces))
    _apply_to_every_piece(
        pieces,
        credentials_for,
        grant_public,
        should_stop=should_stop,
        on_progress=on_progress,
    )

    manifest = Manifest(
        name=file_row.name,
        size_bytes=file_row.size_bytes,
        md5=file_row.md5,
        parts=parts,
    )
    key = new_key()

    # The manifest rides on the account that holds the first piece: one we are
    # certain is linked and healthy, since we just wrote to it.
    manifest_account_id = pieces[0][0]
    credentials = credentials_for(manifest_account_id)
    manifest_id = upload_manifest(credentials, manifest.seal(key))
    grant_public(credentials, manifest_id)

    log.info("shared %r; manifest %s", file_row.name, manifest_id)
    return ShareResult(
        link=encode_link(manifest_id, key),
        manifest_file_id=manifest_id,
        manifest_account_id=manifest_account_id,
        part_count=len(parts),
    )


def unshare_file(
    index,
    file_row,
    credentials_for: CredentialsFor,
    *,
    manifest_file_id: str | None = None,
    manifest_account_id: int | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> None:
    """Withdraw a share: drop the manifest, then unshare every piece.

    The manifest goes first. Someone who already holds the link keeps the
    piece ids regardless of what order this runs in, but a recipient who has
    only just been sent it is stopped at the first step, and that is the more
    common race.
    """
    if manifest_file_id and manifest_account_id is not None:
        credentials = credentials_for(manifest_account_id)
        try:
            service = drive_service(credentials)
            execute_with_backoff(service.files().delete(fileId=manifest_file_id).execute)
        except HttpError as exc:
            if exc.resp.status != 404:
                raise

    _apply_to_every_piece(
        _pieces_of(index, file_row),
        credentials_for,
        revoke_public,
        should_stop=should_stop,
        on_progress=on_progress,
    )
    log.info("withdrew the share for %r", file_row.name)
