"""At-rest encryption for stored credentials.

A single Fernet master key lives in the OS keychain (via `keyring`) — never on
disk. Credential blobs are encrypted with it before being written to the local
SQLite index, so a stolen DB file alone reveals no tokens.
"""

from __future__ import annotations

import keyring
from cryptography.fernet import Fernet

KEYRING_SERVICE = "Gstore"
MASTER_KEY_NAME = "master-key"


# The key is fetched once per process and reused. Each keyring.get_password is a
# separate keychain read, and macOS authorizes reads per requesting binary: an
# app whose signature it cannot match (an unsigned or ad-hoc-signed build, which
# changes identity on every rebuild) gets a password prompt for *each* read. So
# a scan over four accounts meant four prompts. Caching holds that to one read
# per launch. The decrypted tokens live in this process anyway, so keeping their
# key alongside them for the same lifetime reveals nothing new.
_cached_fernet: Fernet | None = None


def _load_master_key() -> bytes:
    """Return the master key from the keychain, creating it on first use."""
    stored = keyring.get_password(KEYRING_SERVICE, MASTER_KEY_NAME)
    if stored is None:
        stored = Fernet.generate_key().decode("ascii")
        keyring.set_password(KEYRING_SERVICE, MASTER_KEY_NAME, stored)
    return stored.encode("ascii")


def _fernet() -> Fernet:
    global _cached_fernet
    if _cached_fernet is None:
        _cached_fernet = Fernet(_load_master_key())
    return _cached_fernet


def reset_cache() -> None:
    """Drop the cached key so the next call re-reads the keychain.

    For tests, and for recovering after the keychain entry is rotated or removed
    underneath a running app.
    """
    global _cached_fernet
    _cached_fernet = None


def encrypt(plaintext: bytes) -> bytes:
    """Encrypt bytes for at-rest storage."""
    return _fernet().encrypt(plaintext)


def decrypt(token: bytes) -> bytes:
    """Decrypt bytes produced by :func:`encrypt`.

    Raises cryptography's InvalidToken if the data is corrupt or the master key
    no longer matches.
    """
    return _fernet().decrypt(token)
