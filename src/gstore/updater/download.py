"""Fetch a release's installer for this machine, and prove it is genuine.

Picks the right asset for the platform the app is running on, downloads it
and its detached signature, and refuses to hand back anything the signature
does not cover. A partially written file is never left where it could be
mistaken for a finished download.

Why this exists at all: finding the correct asset by hand is a real way to
go wrong. The releases page lists three builds, the macOS one runs on Apple
silicon only, and a Windows user who grabs the Linux tarball learns nothing
useful from the error. The app already knows which it needs.

What it deliberately does not do: install anything. The verified file is
handed back for the user to open. Replacing a running application is a
separate problem with its own ways of leaving somebody with no working app.
"""

from __future__ import annotations

import platform
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from gstore.applog import get as get_logger
from gstore.tls import ssl_context
from gstore.updater.signing import (
    SIGNATURE_SUFFIX,
    SignatureError,
    verify_bytes,
)

log = get_logger(__name__)

# Generous next to the update check's 6 seconds: that one runs at startup and
# must never hold the app up, while this one is a 70-100 MB download the user
# explicitly asked for and is watching.
TIMEOUT_SEC = 60
_READ_SIZE = 256 * 1024

__all__ = [
    "DownloadCancelled",
    "DownloadError",
    "SignatureError",
    "UpdateFile",
    "asset_name",
    "download_update",
]


class DownloadError(Exception):
    """The update could not be fetched."""


class DownloadCancelled(Exception):
    """The user stopped the download. Not a failure, and not reported as one."""


@dataclass(frozen=True)
class UpdateFile:
    """A verified installer sitting on disk, ready for the user to open."""

    path: Path
    size_bytes: int
    version: str


def asset_name() -> str | None:
    """The release asset this machine needs, or None if there is not one.

    None is a real answer, not a failure: there has never been an Intel macOS
    build, and someone running on one should be told that plainly rather than
    handed a download that cannot start.
    """
    if sys.platform == "darwin":
        if platform.machine() != "arm64":
            return None
        return "Gstore-macos-arm64.dmg"
    if sys.platform.startswith("win"):
        return "Gstore-windows-x64.zip"
    if sys.platform.startswith("linux"):
        return "Gstore-linux-x64.tar.gz"
    return None


def _fetch(url: str, timeout: int, on_progress=None, should_stop=None) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": "Gstore-updater", "Accept": "*/*"}
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl_context()
        ) as response:
            total = int(response.headers.get("Content-Length") or 0)
            chunks: list[bytes] = []
            read = 0
            while True:
                if should_stop is not None and should_stop():
                    raise DownloadCancelled("the download was stopped")
                block = response.read(_READ_SIZE)
                if not block:
                    break
                chunks.append(block)
                read += len(block)
                if on_progress is not None:
                    # Bytes, not a fraction: the dialog shows "45 MB of 76 MB"
                    # and a speed, and neither can be recovered from a ratio.
                    on_progress(read, total)
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        raise DownloadError(f"the server answered {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DownloadError(f"could not reach {url}: {exc}") from exc


def _asset_urls(assets: list[dict], name: str) -> tuple[str, str]:
    """The download URLs for `name` and its signature, or raise."""
    by_name = {a.get("name"): a.get("browser_download_url") for a in assets}
    url = by_name.get(name)
    if not url:
        raise DownloadError(f"this release has no {name} to download")
    signature = by_name.get(name + SIGNATURE_SUFFIX)
    if not signature:
        raise DownloadError(
            f"{name} has no signature in this release, so it cannot be "
            "checked. Download it from the releases page instead."
        )
    return url, signature


def download_update(
    assets: list[dict],
    version: str,
    destination: Path,
    on_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> UpdateFile:
    """Download this platform's asset into `destination` and verify it.

    `assets` is the release's asset list as GitHub reports it. Raises
    :class:`DownloadError` if it cannot be fetched and
    :class:`SignatureError` if what arrived is not a genuine Gstore release
    — in which case nothing is left behind for a user to open by accident.
    """
    name = asset_name()
    if name is None:
        raise DownloadError(
            "there is no Gstore build for this machine. The macOS build is "
            "Apple silicon only."
        )

    url, signature_url = _asset_urls(assets, name)
    log.info("downloading update %s from %s", name, url)

    signature = _fetch(signature_url, TIMEOUT_SEC, should_stop=should_stop)
    payload = _fetch(
        url, TIMEOUT_SEC, on_progress=on_progress, should_stop=should_stop
    )

    # Verified before it is written, so an unverified installer never exists
    # on disk at all -- not even briefly, under the name a user might open.
    verify_bytes(payload, signature)

    destination.mkdir(parents=True, exist_ok=True)
    target = destination / name
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(payload)
    partial.replace(target)

    log.info("update %s verified and saved to %s", version, target)
    return UpdateFile(path=target, size_bytes=len(payload), version=version)
