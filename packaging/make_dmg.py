"""Packages dist/Gstore.app into a distributable .dmg.

    .venv/bin/pyinstaller packaging/gstore.spec   # build the .app first
    python3 packaging/make_dmg.py

Writes ``dist/Gstore-<version>.dmg``: a compressed read-only image holding the
app plus an /Applications symlink, so the disk image opens to the usual
drag-to-install pair. Uses only `hdiutil`, which ships with macOS.

NOT A SUBSTITUTE FOR SIGNING. The app inside is unsigned and un-notarized, so
Gatekeeper will still tell users the developer "cannot be verified" and they
must right-click -> Open (or allow it in System Settings -> Privacy & Security)
the first time. Fixing that needs an Apple Developer ID, codesign --deep
--options runtime, and notarytool submission; the DMG is just the container.
"""

from __future__ import annotations

import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "dist" / "Gstore.app"


def _app_version(app: Path) -> str:
    """Read the version out of the built bundle, not a source file.

    The DMG should be named after what it actually contains, so a stale build
    cannot be silently shipped under a newer version number.
    """
    plist = app / "Contents" / "Info.plist"
    with plist.open("rb") as fh:
        info = plistlib.load(fh)
    return info.get("CFBundleShortVersionString") or "unknown"


def build_dmg(app: Path = APP) -> Path:
    if not app.is_dir():
        sys.exit(
            f"No app bundle at {app}\n"
            "Build it first: .venv/bin/pyinstaller packaging/gstore.spec"
        )
    if shutil.which("hdiutil") is None:
        sys.exit("hdiutil not found — this script needs macOS.")

    # Include the architecture: an Apple Silicon build cannot run on Intel at
    # all, so the two images must not overwrite each other when both are built
    # and published side by side.
    arch = platform.machine()  # "arm64" or "x86_64"
    version = _app_version(app)
    output = app.parent / f"Gstore-{version}-{arch}.dmg"
    # The published asset is renamed to a stable, version-less filename so the
    # direct-download URL survives new releases — so carry the version in the
    # volume name instead, where the user still sees it on mount.
    volume_name = f"Gstore {version}"

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / volume_name
        staging.mkdir()
        # Copy rather than move so a failed run leaves dist/ untouched.
        # symlinks=True keeps the framework symlinks inside the bundle intact;
        # copytree would otherwise duplicate each Qt library's real file.
        shutil.copytree(app, staging / app.name, symlinks=True)
        (staging / "Applications").symlink_to("/Applications")

        if output.exists():
            output.unlink()  # hdiutil -ov is unreliable across macOS versions
        subprocess.run(
            [
                "hdiutil", "create",
                "-volname", volume_name,
                "-srcfolder", str(staging),
                "-format", "UDZO",  # compressed, read-only
                "-fs", "HFS+",      # widest compatibility for a shipped image
                str(output),
            ],
            check=True, capture_output=True,
        )
    return output


if __name__ == "__main__":
    path = build_dmg()
    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"Wrote {path.relative_to(ROOT)} ({size_mb:.0f} MB)")
    print("Unsigned: users must right-click -> Open on first launch.")
