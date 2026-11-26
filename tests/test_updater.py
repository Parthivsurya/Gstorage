"""Tests for fetching and verifying Gstore's own updates.

This is the one place where the app installs code the user did not pick out
by hand, and a file it downloads itself carries no quarantine mark — so
neither Gatekeeper nor SmartScreen will look at it. The signature is the
only thing standing in their place, which makes these the tests that matter
most in the project.

What they pin down:

* a tampered payload is refused
* a signature from the wrong key is refused
* a release with no signature is refused rather than trusted
* nothing unverified is ever written where a user could open it
"""

from __future__ import annotations

import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from gstore.updater import download as download_mod
from gstore.updater.download import (
    DownloadError,
    asset_name,
    download_update,
)
from gstore.updater.signing import (
    PUBLIC_KEY_HEX,
    SIGNATURE_SUFFIX,
    SignatureError,
    verify_bytes,
    verify_file,
)

PAYLOAD = b"pretend installer bytes" * 100


@pytest.fixture
def real_key(monkeypatch):
    """A key whose public half is the one the app trusts."""
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setattr(
        "gstore.updater.signing.PUBLIC_KEY_HEX", public.hex()
    )
    return key


# -- the signature check -----------------------------------------------------


def test_a_genuine_signature_passes(real_key):
    verify_bytes(PAYLOAD, real_key.sign(PAYLOAD))


def test_a_tampered_payload_is_refused(real_key):
    signature = real_key.sign(PAYLOAD)

    with pytest.raises(SignatureError):
        verify_bytes(PAYLOAD + b"evil", signature)


def test_one_flipped_byte_is_refused(real_key):
    """Not just appended junk: any change at all must fail."""
    signature = real_key.sign(PAYLOAD)
    altered = bytearray(PAYLOAD)
    altered[0] ^= 0x01

    with pytest.raises(SignatureError):
        verify_bytes(bytes(altered), signature)


def test_a_signature_from_another_key_is_refused(real_key):
    """Someone else's valid signature is still not Gstore's."""
    impostor = Ed25519PrivateKey.generate()

    with pytest.raises(SignatureError):
        verify_bytes(PAYLOAD, impostor.sign(PAYLOAD))


def test_a_truncated_signature_is_refused(real_key):
    with pytest.raises(SignatureError):
        verify_bytes(PAYLOAD, real_key.sign(PAYLOAD)[:32])


def test_an_empty_signature_is_refused(real_key):
    with pytest.raises(SignatureError):
        verify_bytes(PAYLOAD, b"")


def test_verification_raises_rather_than_returning_false(real_key):
    """A caller who forgets to check a bool installs an unverified update."""
    assert verify_bytes(PAYLOAD, real_key.sign(PAYLOAD)) is None


def test_the_shipped_public_key_is_a_real_ed25519_key():
    """Guards against a truncated or mangled constant."""
    raw = bytes.fromhex(PUBLIC_KEY_HEX)
    assert len(raw) == 32


def test_verify_file_reads_both_sides(real_key, tmp_path):
    payload = tmp_path / "Gstore.dmg"
    payload.write_bytes(PAYLOAD)
    (tmp_path / ("Gstore.dmg" + SIGNATURE_SUFFIX)).write_bytes(
        real_key.sign(PAYLOAD)
    )

    verify_file(payload, tmp_path / ("Gstore.dmg" + SIGNATURE_SUFFIX))


# -- picking the right build -------------------------------------------------


def test_this_platform_resolves_to_exactly_one_asset():
    name = asset_name()
    if sys.platform == "darwin":
        assert name in {"Gstore-macos-arm64.dmg", None}
    elif sys.platform.startswith("win"):
        assert name == "Gstore-windows-x64.zip"
    elif sys.platform.startswith("linux"):
        assert name == "Gstore-linux-x64.tar.gz"


def test_intel_macs_are_told_there_is_no_build(monkeypatch):
    """There has never been an Intel build; offering one would 404."""
    monkeypatch.setattr(download_mod.sys, "platform", "darwin")
    monkeypatch.setattr(download_mod.platform, "machine", lambda: "x86_64")

    assert asset_name() is None


def test_apple_silicon_gets_the_arm_build(monkeypatch):
    monkeypatch.setattr(download_mod.sys, "platform", "darwin")
    monkeypatch.setattr(download_mod.platform, "machine", lambda: "arm64")

    assert asset_name() == "Gstore-macos-arm64.dmg"


def test_an_unknown_platform_asks_for_nothing(monkeypatch):
    monkeypatch.setattr(download_mod.sys, "platform", "sunos5")

    assert asset_name() is None


# -- the whole download ------------------------------------------------------


def _assets(name: str, *, signed: bool = True) -> list[dict]:
    out = [{"name": name, "browser_download_url": f"https://x/{name}"}]
    if signed:
        out.append(
            {
                "name": name + SIGNATURE_SUFFIX,
                "browser_download_url": f"https://x/{name}{SIGNATURE_SUFFIX}",
            }
        )
    return out


def _serve(monkeypatch, mapping: dict[str, bytes]) -> list[str]:
    """Stand in for the network; returns the list of URLs requested."""
    asked: list[str] = []

    def fake_fetch(url, timeout, on_progress=None, should_stop=None):
        asked.append(url)
        if url not in mapping:
            raise DownloadError(f"404 for {url}")
        if on_progress is not None:
            on_progress(1.0)
        return mapping[url]

    monkeypatch.setattr(download_mod, "_fetch", fake_fetch)
    return asked


def test_a_verified_download_lands_on_disk(real_key, tmp_path, monkeypatch):
    name = asset_name() or "Gstore-macos-arm64.dmg"
    monkeypatch.setattr(download_mod, "asset_name", lambda: name)
    _serve(monkeypatch, {
        f"https://x/{name}": PAYLOAD,
        f"https://x/{name}{SIGNATURE_SUFFIX}": real_key.sign(PAYLOAD),
    })

    result = download_update(_assets(name), "9.9.9", tmp_path)

    assert result.path.read_bytes() == PAYLOAD
    assert result.size_bytes == len(PAYLOAD)
    assert result.version == "9.9.9"


def test_a_tampered_download_leaves_nothing_on_disk(
    real_key, tmp_path, monkeypatch
):
    """The point: no unverified installer may exist for a user to open."""
    name = asset_name() or "Gstore-macos-arm64.dmg"
    monkeypatch.setattr(download_mod, "asset_name", lambda: name)
    _serve(monkeypatch, {
        f"https://x/{name}": PAYLOAD + b"evil",
        f"https://x/{name}{SIGNATURE_SUFFIX}": real_key.sign(PAYLOAD),
    })

    with pytest.raises(SignatureError):
        download_update(_assets(name), "9.9.9", tmp_path)

    assert list(tmp_path.rglob("*")) == [], "something was written anyway"


def test_a_release_with_no_signature_is_refused(tmp_path, monkeypatch):
    """Falling back to 'install it unchecked' would defeat the whole thing."""
    name = asset_name() or "Gstore-macos-arm64.dmg"
    monkeypatch.setattr(download_mod, "asset_name", lambda: name)
    _serve(monkeypatch, {f"https://x/{name}": PAYLOAD})

    with pytest.raises(DownloadError, match="no signature"):
        download_update(_assets(name, signed=False), "9.9.9", tmp_path)


def test_a_release_without_this_platforms_build_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(download_mod, "asset_name", lambda: "Gstore-x.dmg")
    _serve(monkeypatch, {})

    with pytest.raises(DownloadError, match="no Gstore-x.dmg"):
        download_update(_assets("something-else.zip"), "9.9.9", tmp_path)


def test_a_machine_with_no_build_is_told_plainly(tmp_path, monkeypatch):
    monkeypatch.setattr(download_mod, "asset_name", lambda: None)

    with pytest.raises(DownloadError, match="Apple silicon only"):
        download_update(_assets("whatever"), "9.9.9", tmp_path)


def test_the_signature_is_fetched_before_the_payload(
    real_key, tmp_path, monkeypatch
):
    """A missing signature should cost nothing, not a 100 MB download."""
    name = asset_name() or "Gstore-macos-arm64.dmg"
    monkeypatch.setattr(download_mod, "asset_name", lambda: name)
    asked = _serve(monkeypatch, {
        f"https://x/{name}": PAYLOAD,
        f"https://x/{name}{SIGNATURE_SUFFIX}": real_key.sign(PAYLOAD),
    })

    download_update(_assets(name), "9.9.9", tmp_path)

    assert asked[0].endswith(SIGNATURE_SUFFIX), asked


def test_progress_is_reported(real_key, tmp_path, monkeypatch):
    name = asset_name() or "Gstore-macos-arm64.dmg"
    monkeypatch.setattr(download_mod, "asset_name", lambda: name)
    _serve(monkeypatch, {
        f"https://x/{name}": PAYLOAD,
        f"https://x/{name}{SIGNATURE_SUFFIX}": real_key.sign(PAYLOAD),
    })
    seen: list[float] = []

    download_update(_assets(name), "9.9.9", tmp_path, on_progress=seen.append)

    assert seen and max(seen) == pytest.approx(1.0)


def test_no_partial_file_is_left_behind(real_key, tmp_path, monkeypatch):
    name = asset_name() or "Gstore-macos-arm64.dmg"
    monkeypatch.setattr(download_mod, "asset_name", lambda: name)
    _serve(monkeypatch, {
        f"https://x/{name}": PAYLOAD,
        f"https://x/{name}{SIGNATURE_SUFFIX}": real_key.sign(PAYLOAD),
    })

    download_update(_assets(name), "9.9.9", tmp_path)

    assert not list(tmp_path.glob("*.part"))
