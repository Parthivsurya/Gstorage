"""Sign release assets with the project's Ed25519 key.

Run by the release workflow after the artifacts are gathered and before the
GitHub Release is created. Writes `<asset>.sig` next to each asset, holding
the raw 64-byte signature, which is what `gstore.updater.signing` checks
before an in-app update is installed.

The key arrives in GSTORE_SIGNING_KEY as hex. If it is missing the script
exits 0 without signing: a release without signatures is still perfectly
installable by hand, and a missing secret must not take a release down. What
it does mean is that in-app updating will refuse that release, so the
workflow surfaces a warning rather than failing quietly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

KEY_ENV = "GSTORE_SIGNING_KEY"
SIGNATURE_SUFFIX = ".sig"


def load_key(raw_hex: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw_hex.strip()))


def sign_directory(directory: Path, key: Ed25519PrivateKey) -> list[Path]:
    """Sign every asset in `directory`, skipping signatures themselves."""
    written: list[Path] = []
    for asset in sorted(directory.iterdir()):
        if not asset.is_file() or asset.name.endswith(SIGNATURE_SUFFIX):
            continue
        signature = key.sign(asset.read_bytes())
        target = asset.with_name(asset.name + SIGNATURE_SUFFIX)
        target.write_bytes(signature)
        written.append(target)
        print(f"signed {asset.name} -> {target.name} ({len(signature)} bytes)")
    return written


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <directory of assets>", file=sys.stderr)
        return 2

    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        print(
            f"::warning::{KEY_ENV} is not set; this release will be published "
            "unsigned and in-app updating will refuse it."
        )
        return 0

    directory = Path(sys.argv[1])
    if not directory.is_dir():
        print(f"{directory} is not a directory", file=sys.stderr)
        return 1

    key = load_key(raw)
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    # Printed so a mismatch with the key compiled into the app is obvious in
    # the log rather than only as a refused update months later.
    print(f"signing with public key {public.hex()}")

    written = sign_directory(directory, key)
    if not written:
        print("::warning::no assets were signed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
