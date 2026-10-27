"""Read a shared file back, with no account and no credentials.

Nothing here authenticates. Every piece named by a manifest has been marked
readable by anyone holding its id, so a plain HTTPS GET is enough — which is
what lets someone open a share without installing anything but Gstore, and
without owning a Google account at all.

That also means this cannot reuse :mod:`gstore.drive.service`: there are no
credentials to build a service from. It is stdlib HTTP, streamed straight to
disk at the right offset.

The piece checksums in the manifest are load-bearing here, not a nicety.
Unauthenticated Drive endpoints answer failures with an HTML page and a 200,
so "did 50 MB of HTML arrive" is a real failure mode. Every piece is hashed
as it lands and compared, and the whole file is checked again at the end.
"""

from __future__ import annotations

import hashlib
import logging
import os
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from gstore.drive.positional_io import open_binary, pread, pwrite
from gstore.tls import ssl_context
from gstore.share.manifest import Manifest, Part

log = logging.getLogger(__name__)

# No API key: this endpoint serves a file that is readable by anyone with the
# link, which is exactly what a shared piece is. Using the documented
# googleapis.com media endpoint instead would need an API key shipped in the
# app, and one key's quota would then be shared by every Gstore user alive.
PUBLIC_URL = "https://drive.google.com/uc?export=download&id={file_id}"

# Pieces are 50 MB, well under the ~100 MB where Drive interposes a virus-scan
# confirmation page instead of the bytes. Worth stating because it is the
# chunk size that keeps this path simple, not luck.
READ_SIZE = 1024 * 1024
DEFAULT_STREAMS = 4
MAX_STREAMS = 16
STREAMS_ENV = "GSTORE_SHARE_STREAMS"
TIMEOUT_SEC = 60

_USER_AGENT = "Gstore"


class ShareUnavailable(Exception):
    """The shared file could not be read."""


class PieceCorrupt(ShareUnavailable):
    """A piece arrived but was not what the manifest promised."""


def stream_count() -> int:
    raw = os.environ.get(STREAMS_ENV)
    if raw is None:
        return DEFAULT_STREAMS
    try:
        return max(1, min(MAX_STREAMS, int(raw)))
    except ValueError:
        log.warning("%s=%r is not a number; using %d", STREAMS_ENV, raw, DEFAULT_STREAMS)
        return DEFAULT_STREAMS


def _open(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    return urllib.request.urlopen(request, timeout=TIMEOUT_SEC, context=ssl_context())


def fetch_bytes(file_id: str, *, max_bytes: int | None = None) -> bytes:
    """Download a small public file whole — used for the manifest itself."""
    url = PUBLIC_URL.format(file_id=file_id)
    try:
        with _open(url) as response:
            data = response.read(max_bytes) if max_bytes else response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):
            raise ShareUnavailable(
                "that share is no longer available — it may have been "
                "withdrawn, or the file deleted"
            ) from exc
        raise ShareUnavailable(f"could not reach the share ({exc.code})") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ShareUnavailable(f"could not reach Google: {exc}") from exc

    if data[:1] == b"<":
        # An HTML body with a 200 is how these endpoints report trouble.
        raise ShareUnavailable(
            "Google returned a web page instead of the file — the share may "
            "have been withdrawn, or temporarily rate limited"
        )
    return data


def load_manifest(manifest_id: str, key: bytes) -> Manifest:
    """Fetch and decrypt the manifest a link points at."""
    return Manifest.unseal(fetch_bytes(manifest_id), key)


def _fetch_part(part: Part, fd: int, offset: int, should_stop) -> int:
    """Stream one piece to `offset`, verifying it; return bytes written."""
    url = PUBLIC_URL.format(file_id=part.drive_file_id)
    digest = hashlib.md5()
    written = 0

    try:
        with _open(url) as response:
            first = True
            while True:
                if should_stop is not None and should_stop():
                    return written
                block = response.read(READ_SIZE)
                if not block:
                    break
                if first:
                    if block[:1] == b"<":
                        raise ShareUnavailable(
                            "Google returned a web page instead of file data — "
                            "the share may have been withdrawn or rate limited"
                        )
                    first = False
                pwrite(fd, block, offset + written)
                digest.update(block)
                written += len(block)
    except urllib.error.HTTPError as exc:
        raise ShareUnavailable(
            f"a piece of this file is no longer available ({exc.code})"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ShareUnavailable(f"a piece could not be fetched: {exc}") from exc

    if should_stop is not None and should_stop():
        return written

    if written != part.size_bytes:
        raise PieceCorrupt(
            f"a piece arrived {written} bytes long, expected {part.size_bytes}"
        )
    if part.md5 and digest.hexdigest() != part.md5:
        raise PieceCorrupt("a piece did not match its checksum")
    return written


def download_shared(
    manifest: Manifest,
    dest: str | Path,
    *,
    streams: int | None = None,
    should_stop: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Rebuild a shared file at `dest`, verifying every piece and the whole.

    Like an ordinary download, the output is built in a sibling part file and
    moved into place only once the whole-file checksum passes, so `dest` never
    holds partial or unverified data.
    """
    dest = Path(dest)
    part_path = dest.with_name(dest.name + ".gstore-part")
    workers = max(1, min(MAX_STREAMS, streams or stream_count()))

    offsets, running = [], 0
    for piece in manifest.parts:
        offsets.append(running)
        running += piece.size_bytes

    fd = open_binary(part_path, os.O_RDWR | os.O_CREAT)
    done_bytes = 0
    try:
        os.ftruncate(fd, manifest.size_bytes)

        with ThreadPoolExecutor(max_workers=min(workers, len(manifest.parts))) as pool:
            futures = {
                pool.submit(_fetch_part, piece, fd, offset, should_stop): piece
                for piece, offset in zip(manifest.parts, offsets)
            }
            for future in as_completed(futures):
                done_bytes += future.result()
                if on_progress is not None:
                    on_progress(done_bytes, manifest.size_bytes)

        if should_stop is not None and should_stop():
            raise ShareUnavailable("the download was cancelled")

        if manifest.md5:
            if _file_md5(fd, manifest.size_bytes) != manifest.md5:
                raise PieceCorrupt(
                    "the rebuilt file does not match the sender's checksum"
                )
    finally:
        os.close(fd)

    os.replace(part_path, dest)
    log.info("rebuilt %r from %d piece(s)", dest.name, len(manifest.parts))
    return dest


def _file_md5(fd: int, size: int) -> str:
    """Hash the rebuilt file through the portable shim.

    Via positional_io, not os.pread: there is no os.pread on Windows, and
    reaching for it directly is what broke the build three times over.
    """
    digest = hashlib.md5()
    offset = 0
    while offset < size:
        block = pread(fd, min(READ_SIZE, size - offset), offset)
        if not block:
            break
        digest.update(block)
        offset += len(block)
    return digest.hexdigest()
