"""Application paths and constants.

Everything Gstore writes lives under the OS-native app-data directory so we
never scatter state in the working tree or the user's home root.
"""

from __future__ import annotations

import os
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


# Name of the OAuth "Desktop app" client-secrets file the user downloads from
# Google Cloud Console. Looked for in the app-data dir unless overridden.
CLIENT_SECRETS_FILENAME = "client_secret.json"
CLIENT_SECRETS_ENV = "GSTORE_CLIENT_SECRETS"


def client_secrets_path() -> Path:
    """Path to the OAuth client-secrets JSON.

    Honors the GSTORE_CLIENT_SECRETS env var; otherwise looks in the app-data
    dir. The file's presence is not guaranteed here — callers should check.
    """
    override = os.environ.get(CLIENT_SECRETS_ENV)
    if override:
        return Path(override).expanduser()
    return data_dir() / CLIENT_SECRETS_FILENAME
