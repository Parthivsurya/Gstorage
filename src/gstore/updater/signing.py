"""Verify that a downloaded update really came from this project.

Gstore's builds are not code-signed, so the operating system vouches for
nothing. That is survivable while a human downloads each release through a
browser — the browser marks the file, and Gatekeeper or SmartScreen puts the
decision in front of them.

An update the app fetches and installs **itself** carries no such mark and
raises no such prompt, which is exactly what makes it pleasant and exactly
what makes it dangerous: the operating system steps out of the way, so this
module has to stand in its place. Without it, anyone able to interfere with
the download could put code straight onto a user's machine with nothing
asked.

This needs no certificate authority and costs nothing. A code signing
certificate exists to prove an identity to *the OS*; this proves to *the app*
that a file is the one its author built, which only needs a key pair the
author holds. The private half lives in a GitHub Actions secret and signs
each asset at release time; the public half is the constant below.

Ed25519 via `cryptography`, already a dependency for the token keystore — no
new package, no new supply chain.
"""

from __future__ import annotations

from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from gstore.applog import get as get_logger

log = get_logger(__name__)

# The public half of the release signing key. Safe to publish — it can only
# check signatures, never make them. Changing it invalidates every signature
# made with the old key, so it is replaced only if the private half is lost
# or compromised, and then every release has to be re-signed.
PUBLIC_KEY_HEX = "d9b5888a11349f23f893698e77ba67feca4edcc518217b5a540a1798c98584fb"

# Each release asset is published alongside `<asset name>.sig`, holding the
# raw 64-byte Ed25519 signature of the asset's bytes. No container format:
# there is nothing to parse and so nothing to parse wrongly.
SIGNATURE_SUFFIX = ".sig"
SIGNATURE_BYTES = 64

__all__ = [
    "PUBLIC_KEY_HEX",
    "SIGNATURE_SUFFIX",
    "SignatureError",
    "verify_bytes",
    "verify_file",
]


class SignatureError(Exception):
    """A download could not be shown to be a genuine Gstore release."""


def _public_key() -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(PUBLIC_KEY_HEX))


def verify_bytes(payload: bytes, signature: bytes) -> None:
    """Raise :class:`SignatureError` unless `signature` covers `payload`.

    Raises rather than returning False on purpose. A caller that forgets to
    check a boolean installs an unverified update; one that forgets to catch
    an exception crashes instead, which is the better failure.
    """
    if len(signature) != SIGNATURE_BYTES:
        raise SignatureError(
            f"the signature is {len(signature)} bytes, expected "
            f"{SIGNATURE_BYTES} — the download is not a Gstore release"
        )
    try:
        _public_key().verify(signature, payload)
    except InvalidSignature:
        raise SignatureError(
            "this download was not signed by Gstore's release key. It may "
            "have been altered in transit, or served by something other than "
            "the real release. It has not been installed."
        ) from None


def verify_file(path: str | Path, signature_path: str | Path) -> None:
    """Verify a downloaded file against its `.sig`, reading both from disk."""
    payload = Path(path).read_bytes()
    signature = Path(signature_path).read_bytes()
    verify_bytes(payload, signature)
    log.info("verified the signature on %s", Path(path).name)
