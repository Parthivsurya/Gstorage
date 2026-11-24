"""Move files Gstore uploaded earlier into the account's Gstore folder.

Everything uploaded before the folder existed sits loose in My Drive's root, so
somebody upgrading finds half their pieces tidy and half not. This is the
one-time catch-up for that split.

It moves **metadata, never bytes.** ``files.update`` with addParents /
removeParents rewrites a parent pointer, so a 20 GB file's ~400 pieces are 400
small requests and not one byte is re-uploaded. The Drive file id is unchanged
by a move, which is the reason this needs no index update at all and why every
live share link keeps working through it.

Safe to stop and safe to repeat: each file is moved independently, so an
interrupted run leaves the files it already moved inside the folder and the
rest where they were, and running it again simply picks up the remainder.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable

from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError

from gstore.applog import get as get_logger
from gstore.drive.folder import FOLDER_MIME
from gstore.drive.rate_limit import execute_with_backoff
from gstore.drive.service import drive_service

# Metadata calls are cheap and latency-bound, so they parallelise well. Matches
# the share code's permission workers for the same reason.
TIDY_WORKERS = 8

log = get_logger(__name__)

__all__ = ["Misplaced", "TidyResult", "files_to_move", "move_into", "tidy_account"]


@dataclass(frozen=True)
class Misplaced:
    """An app-created file that is not in the Gstore folder yet."""

    drive_file_id: str
    name: str
    parents: tuple[str, ...]


@dataclass(frozen=True)
class TidyResult:
    moved: int
    failed: int


class Cancelled(Exception):
    """The user stopped a tidy-up partway."""


def files_to_move(credentials: Credentials, folder_id: str) -> list[Misplaced]:
    """App-created files on this account that do not live in `folder_id`.

    Folders are excluded: the Gstore folder is app-created too, and moving it
    inside itself is not a thing Drive will do.
    """
    service = drive_service(credentials)
    found: list[Misplaced] = []
    page_token: str | None = None
    while True:
        response = execute_with_backoff(
            lambda token=page_token: service.files()
            .list(
                q=f"trashed=false and mimeType != '{FOLDER_MIME}'",
                spaces="drive",
                fields="nextPageToken, files(id, name, parents)",
                pageSize=1000,
                pageToken=token,
            )
            .execute()
        )
        for entry in response.get("files", []):
            parents = tuple(entry.get("parents") or ())
            if folder_id in parents:
                continue
            found.append(
                Misplaced(
                    drive_file_id=entry["id"],
                    name=entry.get("name", ""),
                    parents=parents,
                )
            )
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return found


def move_into(credentials: Credentials, file: Misplaced, folder_id: str) -> None:
    """Re-parent one file into `folder_id`.

    Every existing parent is removed, not just the root. Drive used to allow a
    file in several folders at once, so an older file can carry more than one
    and leaving them attached would show it in two places.
    """
    service = drive_service(credentials)
    request = service.files().update(
        fileId=file.drive_file_id,
        addParents=folder_id,
        removeParents=",".join(file.parents) if file.parents else None,
        fields="id",
    )
    execute_with_backoff(request.execute)


def tidy_account(
    credentials: Credentials,
    folder_id: str,
    *,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> TidyResult:
    """Move every misplaced file on this account into `folder_id`.

    A file that fails is counted and skipped rather than aborting the run: one
    file Drive will not move is no reason to leave the other 399 scattered. The
    count comes back so the caller can say so.
    """
    pending = files_to_move(credentials, folder_id)
    total = len(pending)
    if not total:
        return TidyResult(moved=0, failed=0)

    log.info("tidying %d file(s) into %s", total, folder_id)
    done = moved = failed = 0

    with ThreadPoolExecutor(max_workers=min(TIDY_WORKERS, total)) as pool:
        futures = {
            pool.submit(move_into, credentials, file, folder_id): file
            for file in pending
        }
        for future in as_completed(futures):
            if should_stop is not None and should_stop():
                for waiting in futures:
                    waiting.cancel()
                raise Cancelled("the tidy-up was stopped")
            file = futures[future]
            try:
                future.result()
                moved += 1
            except (HttpError, OSError) as exc:
                failed += 1
                log.warning("could not move %r: %s", file.name, exc)
            done += 1
            if on_progress is not None:
                on_progress(done, total)

    log.info("tidied %d file(s), %d could not be moved", moved, failed)
    return TidyResult(moved=moved, failed=failed)
