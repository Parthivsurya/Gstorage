"""Application paths and constants.

Everything Gstore writes lives under the OS-native app-data directory so we
never scatter state in the working tree or the user's home root.
"""

from __future__ import annotations

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
