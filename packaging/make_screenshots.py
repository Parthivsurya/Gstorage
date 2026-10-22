"""Render the README screenshots.

    .venv/bin/python packaging/make_screenshots.py

Writes assets/screenshots/{files-list,storage}.png.

The UI is driven against a throwaway SQLite index seeded with obviously-fake
``@example.com`` accounts, so no real address, token or file ever reaches a
published image, and the real app-data database is never opened. Qt grabs its
own widgets, so this needs no screen recording and no network — but it does
render with the host's native widget style and light/dark setting, so the
output reflects whatever machine it ran on.

Set GSTORE_SHOT_OFFSCREEN=1 to render headless (CI, no display). That falls
back to Qt's generic theme rather than the native one.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

if os.environ.get("GSTORE_SHOT_OFFSCREEN"):
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_SCALE_FACTOR"] = "2"  # match a retina grab's pixel size

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "screenshots"

GB = 1024**3
MB = 1024 * 1024

# Window size per shot, chosen so each tab's content fills the frame without
# clipping the last row.
SIZES = {"files-list.png": (880, 395), "storage.png": (880, 530)}

from gstore.ui import storage_panel as sp_mod  # noqa: E402

# StoragePanel.__init__ kicks off a network quota refresh; neutralise it so the
# seeded snapshots are what gets drawn.
sp_mod.StoragePanel._start_quota_refresh = lambda self: None

from PIL import Image  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gstore.db import Database  # noqa: E402
from gstore.index import IndexStore  # noqa: E402
from gstore.ui.main_window import MainWindow  # noqa: E402

ACCOUNTS = [
    ("demo.one@example.com", 15 * GB, int(13.4 * GB)),
    ("demo.two@example.com", 15 * GB, int(6.2 * GB)),
    ("demo.three@example.com", 15 * GB, int(1.1 * GB)),
]

ROOT_FILES = [
    ("Wedding-2026-raw.zip", 8_400 * MB, 0),
    ("site-backup-2026-10.tar.gz", 3_100 * MB, 1),
    ("Interview-master.mov", 1_950 * MB, 2),
    ("tax-return-2025.pdf", 2 * MB, 1),
    ("passport-scan.png", 6 * MB, 2),
]

PHOTO_FILES = [
    ("IMG_4821.HEIC", 4 * MB, 0),
    ("IMG_4822.HEIC", 5 * MB, 0),
    ("hike-timelapse.mp4", 740 * MB, 1),
]


def seed(db: Database) -> None:
    """Fill a fresh index with demo accounts, folders and files."""
    ids = []
    for email, limit, used in ACCOUNTS:
        cur = db.conn.execute(
            "INSERT INTO accounts(email, quota_limit_bytes, quota_used_bytes, "
            "quota_synced_at, status) VALUES(?,?,?,datetime('now'),'active')",
            (email, limit, used),
        )
        ids.append(int(cur.lastrowid))
    db.conn.commit()

    index = IndexStore(db)
    index.create_folder("Archive")
    photos = index.create_folder("Photos")
    index.create_folder("Projects")

    for name, size, who in ROOT_FILES:
        index.create_file(name, None, size, ids[who], f"drive-{name}", md5="0" * 32)
    for name, size, who in PHOTO_FILES:
        index.create_file(name, photos, size, ids[who], f"drive-{name}", md5="0" * 32)


def trim_bottom(image: Image.Image, margin: int = 10, pad: int = 26) -> Image.Image:
    """Crop uniform background rows off the bottom, keeping a little padding.

    A grab is as tall as the window, which leaves dead space under the button
    row. `margin` skips the window border, which is not background-coloured.
    """
    rgb = image.convert("RGB")
    bg = rgb.getpixel((rgb.width // 2, rgb.height - margin - 1))
    y = rgb.height - margin - 1
    while y > 0:
        row = (rgb.getpixel((x, y)) for x in range(margin, rgb.width - margin, 5))
        if any(sum(abs(c[i] - bg[i]) for i in range(3)) > 24 for c in row):
            break
        y -= 1
    return image.crop((0, 0, image.width, min(image.height, y + pad)))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="gstore-shots-"))
    db = Database(tmp / "demo.db").open().initialize()
    seed(db)

    app = QApplication(sys.argv[:1])
    window = MainWindow(db)
    window.show()
    tabs = window.centralWidget()

    def shoot(name: str, tab: int) -> None:
        tabs.setCurrentIndex(tab)
        window.resize(*SIZES[name])
        app.processEvents()
        app.processEvents()
        path = OUT / name
        if not window.grab().save(str(path), "PNG"):
            raise SystemExit(f"failed to save {path}")
        trim_bottom(Image.open(path)).save(path, optimize=True)
        print(f"wrote {path.relative_to(ROOT)}")

    window.file_browser.refresh()
    shoot("files-list.png", 0)
    window.storage_panel.reload()
    shoot("storage.png", 1)

    db.close()
    print("Now run: oxipng -o max --strip safe -a assets/screenshots/*.png")


if __name__ == "__main__":
    main()
