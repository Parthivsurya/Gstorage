"""Check GitHub for a newer release.

Gstore is distributed as unsigned downloads rather than through an app store,
so nothing tells an existing user that a new version exists. This asks the
public releases endpoint for the latest tag and compares it to the running
version.

What leaves the machine: one unauthenticated GET to api.github.com. No account
information, no file names, no tokens, no identifier of any kind — only the
IP address any HTTP request carries. It is the sole thing Gstore ever sends
anywhere other than Google, it can be turned off, and it is described in
PRIVACY.md.

stdlib urllib rather than requests: requests is only present here as a
transitive dependency of google-auth, and an unauthenticated GET does not
justify depending on it directly.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from gstore.tls import ssl_context
from dataclasses import dataclass

from gstore.applog import get as get_logger

log = get_logger(__name__)

OWNER_REPO = "Parthivsurya/Gstorage"
RELEASES_API = f"https://api.github.com/repos/{OWNER_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{OWNER_REPO}/releases/latest"

# Short: this runs at startup and must never hold the app up. Failing is fine,
# a missed check costs nothing and the next launch tries again.
TIMEOUT_SEC = 6

_VERSION_RE = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass(frozen=True)
class Release:
    """A release newer than the one running."""

    version: str
    url: str
    notes: str


def parse_version(text: str) -> tuple[int, int, int] | None:
    """Turn 'v0.1.2', '0.1.2', 'Gstore 1.2' into a comparable triple."""
    if not text:
        return None
    match = _VERSION_RE.search(text.strip())
    if match is None:
        return None
    major, minor, patch = match.groups()
    return (int(major), int(minor or 0), int(patch or 0))


def is_newer(candidate: str, current: str) -> bool:
    """Whether `candidate` is a later version than `current`.

    Unparseable input is treated as "not newer": a malformed tag must never
    nag a user into chasing a release that may not exist.
    """
    new = parse_version(candidate)
    now = parse_version(current)
    if new is None or now is None:
        return False
    return new > now


def fetch_latest(timeout: int = TIMEOUT_SEC) -> dict | None:
    """The latest release as GitHub reports it, or None if that failed."""
    request = urllib.request.Request(
        RELEASES_API,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "Gstore-update-check",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl_context()
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        # Offline, rate-limited, or GitHub having a bad day. None of that is
        # the user's problem and none of it should surface in the UI.
        log.info("update check did not complete: %s: %s", type(exc).__name__, exc)
        return None


def check_for_update(current_version: str, timeout: int = TIMEOUT_SEC) -> Release | None:
    """Return the newer release if there is one, else None."""
    payload = fetch_latest(timeout=timeout)
    if not payload or payload.get("draft") or payload.get("prerelease"):
        return None

    tag = payload.get("tag_name") or ""
    if not is_newer(tag, current_version):
        return None

    log.info("update available: %s (running %s)", tag, current_version)
    return Release(
        version=tag.lstrip("v"),
        url=payload.get("html_url") or RELEASES_PAGE,
        notes=(payload.get("body") or "").strip(),
    )
