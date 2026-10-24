"""Application paths and constants.

Everything Gstore writes lives under the OS-native app-data directory so we
never scatter state in the working tree or the user's home root.
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path

from platformdirs import PlatformDirs

APP_NAME = "Gstore"
APP_AUTHOR = "Gstore"

_dirs = PlatformDirs(appname=APP_NAME, appauthor=APP_AUTHOR)


def data_dir() -> Path:
    """OS app-data directory, created on first access.

    macOS:   ~/Library/Application Support/Gstore
    Linux:   ~/.local/share/Gstore
    Windows: %LOCALAPPDATA%\\Gstore
    """
    path = Path(_dirs.user_data_dir)
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    """Absolute path to the local SQLite index."""
    return data_dir() / "gstore.db"


# Opt-in "whole Drive" power mode.
#
# By default Gstore asks Google only for `drive.file`, which is not a restricted
# scope: anyone can sign in, see their pooled storage and manage the files
# Gstore itself placed, with no Google CASA security assessment required of the
# app. Seeing files Gstore did *not* create (the combined "Scan Drive now" view)
# needs the restricted `drive` scope, which Google only grants a verified app
# holding a current CASA assessment — so it is opt-in rather than demanded of
# every user at sign-in.
#
# Enabled by the GSTORE_FULL_DRIVE env var (development) or by the presence of a
# marker file in the app-data dir (end users), so no settings UI is needed yet.
# Changing it only affects accounts linked *afterwards*: an existing token keeps
# whatever scopes it was granted until the account is re-linked.
FULL_DRIVE_ENV = "GSTORE_FULL_DRIVE"
FULL_DRIVE_FLAG_FILENAME = "full_drive.enabled"

_TRUTHY = {"1", "true", "yes", "on"}


def full_drive_requested() -> bool:
    """Whether to ask for the restricted `drive` scope at next sign-in."""
    override = os.environ.get(FULL_DRIVE_ENV)
    if override is not None:
        return override.strip().lower() in _TRUTHY
    return (data_dir() / FULL_DRIVE_FLAG_FILENAME).exists()


def set_full_drive_requested(enabled: bool) -> None:
    """Persist the opt-in for future sign-ins (ignores the env override)."""
    flag = data_dir() / FULL_DRIVE_FLAG_FILENAME
    if enabled:
        flag.touch()
    elif flag.exists():
        flag.unlink()


# OAuth "Desktop app" client-secrets file. End users no longer supply their
# own: the app ships a bundled client (see gstore/auth/client_config.json). The
# per-user file and env var remain as overrides — mainly for development and
# for anyone who wants to point Gstore at their own Google Cloud project.
CLIENT_SECRETS_FILENAME = "client_secret.json"
CLIENT_SECRETS_ENV = "GSTORE_CLIENT_SECRETS"

# Bundled Desktop-app client shipped inside the package. A Desktop/PKCE client
# has no confidential secret to protect, so this is the standard way to let end
# users just click "Sign in with Google" without any Cloud Console setup.
BUNDLED_CLIENT_RESOURCE = ("gstore.auth", "client_config.json")


def client_secrets_path() -> Path | None:
    """Path to a user-supplied OAuth client-secrets JSON, or ``None``.

    Only covers the real-file overrides — the ``GSTORE_CLIENT_SECRETS`` env var
    and a bring-your-own file in the app-data dir. The bundled client is read
    separately via :func:`read_bundled_client_config`, since a packaged resource
    is not guaranteed to have a stable filesystem path.
    """
    override = os.environ.get(CLIENT_SECRETS_ENV)
    if override:
        path = Path(override).expanduser()
        if path.exists():
            return path

    user_file = data_dir() / CLIENT_SECRETS_FILENAME
    if user_file.exists():
        return user_file

    return None


def read_bundled_client_config() -> dict | None:
    """Parse the Desktop client shipped inside the package, or ``None``.

    Reads the resource directly (works in a normal install and inside a frozen
    build), so no temp file is materialized.
    """
    import json

    package, name = BUNDLED_CLIENT_RESOURCE
    resource = resources.files(package) / name
    if not resource.is_file():
        return None
    return json.loads(resource.read_text(encoding="utf-8"))


# Update checking.
#
# Gstore ships as unsigned downloads, so without this nothing tells an existing
# user that a newer version exists. It is the only request the app makes to
# anywhere other than Google — one unauthenticated GET to GitHub's public
# releases endpoint, carrying no account data — and it is disclosed in
# PRIVACY.md. Users who would rather it did not happen can switch it off, which
# leaves a marker file here.
UPDATE_CHECK_ENV = "GSTORE_NO_UPDATE_CHECK"
UPDATE_CHECK_FLAG_FILENAME = "update_check.disabled"


def update_check_enabled() -> bool:
    """Whether to ask GitHub for a newer release at startup."""
    override = os.environ.get(UPDATE_CHECK_ENV)
    if override is not None:
        return override.strip().lower() not in _TRUTHY
    return not (data_dir() / UPDATE_CHECK_FLAG_FILENAME).exists()


def set_update_check_enabled(enabled: bool) -> None:
    """Persist the choice (ignores the env override)."""
    flag = data_dir() / UPDATE_CHECK_FLAG_FILENAME
    if enabled:
        flag.unlink(missing_ok=True)
    else:
        flag.touch()


# Releases the user has chosen to skip, one version per line, so a declined
# update does not nag on every launch.
SKIPPED_VERSIONS_FILENAME = "skipped_versions.txt"


def skipped_versions() -> set[str]:
    path = data_dir() / SKIPPED_VERSIONS_FILENAME
    if not path.exists():
        return set()
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def skip_version(version: str) -> None:
    path = data_dir() / SKIPPED_VERSIONS_FILENAME
    versions = skipped_versions() | {version.strip()}
    path.write_text("\n".join(sorted(versions)) + "\n")
