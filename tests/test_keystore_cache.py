"""Tests that the master key is read from the keychain once per process.

Each keyring read is a separate keychain authorization, and macOS authorizes
per requesting binary — a build whose signature it cannot match prompts the user
for their login password on *every* read. Re-reading per credential meant one
prompt per account touched, so the caching here is what keeps a multi-account
scan to a single prompt.
"""

from __future__ import annotations

from unittest import mock

import keyring
import pytest
from cryptography.fernet import Fernet

from gstore.auth import keystore


@pytest.fixture
def fake_keychain():
    """Serve a fixed master key and count how often the keychain is read."""
    keystore.reset_cache()
    key = Fernet.generate_key().decode("ascii")
    with mock.patch.object(keyring, "get_password", return_value=key) as spy:
        yield spy
    keystore.reset_cache()


def test_many_operations_read_the_keychain_once(fake_keychain):
    for _ in range(5):
        keystore.decrypt(keystore.encrypt(b"secret"))
    assert fake_keychain.call_count == 1


def test_reset_cache_forces_a_rereads(fake_keychain):
    keystore.encrypt(b"a")
    keystore.reset_cache()
    keystore.encrypt(b"b")
    assert fake_keychain.call_count == 2


def test_roundtrip_still_works_through_the_cache(fake_keychain):
    assert keystore.decrypt(keystore.encrypt(b"payload")) == b"payload"


def test_key_is_generated_and_stored_when_absent():
    keystore.reset_cache()
    with mock.patch.object(keyring, "get_password", return_value=None), \
            mock.patch.object(keyring, "set_password") as setter:
        keystore.encrypt(b"first run")
    assert setter.call_count == 1
    service, name, value = setter.call_args[0]
    assert (service, name) == (keystore.KEYRING_SERVICE, keystore.MASTER_KEY_NAME)
    Fernet(value.encode("ascii"))  # must be a usable Fernet key
    keystore.reset_cache()
