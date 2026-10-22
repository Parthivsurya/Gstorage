"""Builds the platform app icons from the 1024px logo master.

    python3 packaging/make_icon.py

Writes ``assets/Gstore.icns`` (macOS) and ``assets/Gstore.ico`` (Windows), both
consumed by ``packaging/gstore.spec``. Each format wants every size baked in
rather than scaled at display time — a lone 1024px image renders blurry in the
Dock and pixelated in Finder and the Windows taskbar — so every required size is
resampled from the master.

The .icns needs macOS (`sips`, `iconutil`, both built in). The .ico only needs
Pillow, so it can be produced anywhere, which matters because the Windows build
runs on a machine that has no iconutil. Re-run after changing the logo.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MASTER = ROOT / "assets" / "gstore_logo_1024.png"
ICNS_OUTPUT = ROOT / "assets" / "Gstore.icns"
ICO_OUTPUT = ROOT / "assets" / "Gstore.ico"

# Sizes Windows picks between for the taskbar, Explorer and Alt-Tab.
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

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


def build_icns(master: Path = MASTER, output: Path = ICNS_OUTPUT) -> Path:
    """Resample the master into every required size and assemble the .icns."""
    if not master.is_file():
        sys.exit(f"Logo master not found: {master}\nSee assets/README.md.")
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


def build_ico(master: Path = MASTER, output: Path = ICO_OUTPUT) -> Path:
    """Write a multi-resolution .ico. Pillow embeds each size in one file."""
    from PIL import Image

    if not master.is_file():
        sys.exit(f"Logo master not found: {master}\nSee assets/README.md.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(master) as img:
        img.convert("RGBA").save(output, format="ICO", sizes=ICO_SIZES)
    return output


if __name__ == "__main__":
    built = []
    if sys.platform == "darwin":
        built.append(build_icns())
    else:
        print("Not macOS — skipping the .icns (needs iconutil).")
    built.append(build_ico())
    for path in built:
        print(f"Wrote {path.relative_to(ROOT)} ({path.stat().st_size:,} bytes)")
