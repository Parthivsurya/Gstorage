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
