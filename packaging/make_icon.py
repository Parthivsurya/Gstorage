"""Builds the macOS .icns app icon from the 1024px logo master.

    python3 packaging/make_icon.py

Writes ``assets/Gstore.icns``, which ``packaging/gstore.spec`` hands to
PyInstaller's BUNDLE. macOS wants every size baked into the .icns rather than
scaled at display time — a single 1024px image shows up blurry in the Dock and
pixelated in Finder's list view — so each required size is resampled from the
master with `sips` and assembled with `iconutil`.

Both tools ship with macOS; nothing to install. Re-run after changing the logo.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "assets" / "gstore_logo_1024.png"
OUTPUT = ROOT / "assets" / "Gstore.icns"

# (pixel size, filename) pairs iconutil requires. The @2x variants carry the
# same pixels as the next size up — that is how Retina density is declared.
VARIANTS = [
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]


def _require_tool(name: str) -> None:
    if shutil.which(name) is None:
        sys.exit(f"{name} not found — this script needs macOS (sips, iconutil).")


def build_icns(master: Path = MASTER, output: Path = OUTPUT) -> Path:
    """Resample the master into every required size and assemble the .icns."""
    if not master.is_file():
        sys.exit(f"Logo master not found: {master}\nRun packaging/make_logo.py first.")
    for tool in ("sips", "iconutil"):
        _require_tool(tool)

    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "Gstore.iconset"
        iconset.mkdir()
        for size, name in VARIANTS:
            subprocess.run(
                ["sips", "-z", str(size), str(size), str(master),
                 "--out", str(iconset / name)],
                check=True, capture_output=True,
            )
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["iconutil", "--convert", "icns", str(iconset), "--output", str(output)],
            check=True, capture_output=True,
        )
    return output


if __name__ == "__main__":
    path = build_icns()
    print(f"Wrote {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")
