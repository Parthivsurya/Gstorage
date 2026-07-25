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


def _load_master_key() -> bytes:
    """Return the master key from the keychain, creating it on first use."""
    stored = keyring.get_password(KEYRING_SERVICE, MASTER_KEY_NAME)
    if stored is None:
        stored = Fernet.generate_key().decode("ascii")
        keyring.set_password(KEYRING_SERVICE, MASTER_KEY_NAME, stored)
    return stored.encode("ascii")


def _fernet() -> Fernet:
    return Fernet(_load_master_key())


def encrypt(plaintext: bytes) -> bytes:
    """Encrypt bytes for at-rest storage."""
    return _fernet().encrypt(plaintext)


def decrypt(token: bytes) -> bytes:
    """Decrypt bytes produced by :func:`encrypt`.

    Raises cryptography's InvalidToken if the data is corrupt or the master key
    no longer matches.
    """
    return _fernet().decrypt(token)
