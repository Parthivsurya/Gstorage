"""Render a 9:16 story card explaining what Gstore is.

    .venv/bin/python packaging/make_poster.py

Writes assets/social/release-<version>-story.png at 1080x1920, the size
Instagram, WhatsApp and Threads stories expect. make_release_card.py renders
a different design, dark and notes-shaped; both are kept.

Deliberately plain: white ground, black text, left aligned, no gradients or
ornament. Someone seeing this once while scrolling should finish it knowing
what the app is, what they would do with it and what is inside, so the page
is packed rather than airy -- labelled sections, bold lead-ins to scan by, no
decorative space. The logo sits top right so the whole left edge stays a text
column.

Two things the wording has to keep getting right. "One drive" is avoided
throughout: it reads as Microsoft OneDrive. And splitting is described as the
fallback it actually is -- a file that fits goes whole to one account, and
only a file too big for any single account is cut into 50 MB pieces -- which
is also the one idea the diagram is here to carry.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cardkit import font, rich                              # noqa: E402

from gstore import __version__                              # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "social"

W, H = 1080, 1920
M = 74                      # side margin
COL = W - 2 * M

PAPER = (255, 255, 255)
INK = (17, 20, 24)
MUTED = (104, 116, 130)
RULE = (214, 220, 227)
ACCENT = (21, 101, 192)

FILLS = {"r": INK, "b": INK, "m": MUTED, "l": ACCENT}
SOFT = {"r": MUTED, "b": INK, "m": MUTED, "l": ACCENT}

BODY, LEAD = 29, 25


def centred(draw, box, y, text, f, fill=INK) -> None:
    x0, x1 = box
    w = draw.textbbox((0, 0), text, font=f)[2]
    draw.text((x0 + (x1 - x0 - w) / 2, y), text, font=f, fill=fill)


def section(draw, y: int, label: str) -> int:
    """A hairline and a small caps label: free structure, almost no height."""
    draw.line([M, y, W - M, y], fill=RULE, width=2)
    f = font(22, "Semibold")
    draw.text((M, y + 17), label.upper(), font=f, fill=MUTED)
    return y + 17 + draw.textbbox((0, 0), label.upper(), font=f)[3] + 19


def bullets(draw, y: int, items, gap: int = 15) -> int:
    for runs in items:
        draw.ellipse([M + 2, y + 11, M + 11, y + 20], fill=ACCENT)
        y = rich(draw, M + 32, y, runs, LEAD, COL - 32, FILLS, leading=6) + gap
    return y - gap


def steps(draw, y: int, items, gap: int = 15) -> int:
    nf = font(LEAD, "Bold")
    for i, runs in enumerate(items, 1):
        draw.text((M, y), str(i), font=nf, fill=ACCENT)
        y = rich(draw, M + 32, y, runs, LEAD, COL - 32, FILLS, leading=6) + gap
    return y - gap


def diagram(draw, y: int) -> int:
    """The fallback case, which is the hard one to say in a sentence.

    Drawn as a branch rather than a single arrow: one box wide enough to read
    as "bigger than any of the three", a stem, and an arrowhead into each
    account. It costs about 260px of height, which is why the sections around
    it are written tight.
    """
    bar_h = 78
    draw.rounded_rectangle([M, y, W - M, y + bar_h], radius=12,
                           outline=INK, width=3)
    centred(draw, (M, W - M), y + 20, "one 20 GB file", font(34, "Semibold"))
    y += bar_h

    gap = 16
    cell_w = (COL - 2 * gap) // 3
    centres = [M + i * (cell_w + gap) + cell_w // 2 for i in range(3)]
    mid, drop = W // 2, 42
    draw.line([mid, y, mid, y + drop], fill=INK, width=3)
    draw.line([centres[0], y + drop, centres[-1], y + drop], fill=INK, width=3)
    for cx in centres:
        draw.line([cx, y + drop, cx, y + drop + 26], fill=INK, width=3)
        draw.polygon([(cx - 9, y + drop + 23), (cx + 9, y + drop + 23),
                      (cx, y + drop + 41)], fill=INK)
    draw.text((mid + 20, y + 6), "split into 50 MB pieces",
              font=font(25), fill=MUTED)
    y += drop + 45

    cell_h = 94
    for i, cx in enumerate(centres):
        x0 = cx - cell_w // 2
        draw.rounded_rectangle([x0, y, x0 + cell_w, y + cell_h], radius=12,
                               outline=RULE, width=3)
        centred(draw, (x0, x0 + cell_w), y + 17, f"account {i + 1}",
                font(23), MUTED)
        centred(draw, (x0, x0 + cell_w), y + 49, "15 GB", font(31, "Semibold"))
    return y + cell_h


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (W, H), PAPER)
    draw = ImageDraw.Draw(img)

    # --- identity: text down the left, logo parked top right ---------------
    logo = Image.open(ROOT / "assets" / "gstore_logo_512.png").convert("RGBA")
    logo = logo.resize((96, 96), Image.LANCZOS)
    img.paste(logo, (W - M - 96, 54), logo)
    draw.text((M, 54), "Gstore", font=font(44, "Bold"), fill=INK)
    draw.text((M, 108), "pooled Google Drive storage  ·  "
              f"version {__version__}", font=font(24), fill=MUTED)

    y = rich(draw, M, 176, [("Pool the free space in all your "
                             "Google accounts.", "b")],
             56, COL, FILLS, leading=4)

    y = rich(draw, M, y + 20, [
        ("Every Google account comes with 15 GB free. Three accounts is "
         "45 GB — but as three separate silos. Gstore lays ", "r"),
        ("one folder tree", "b"),
        (" over all of them and remembers where everything went.", "r"),
    ], BODY, COL, SOFT, leading=8)

    # --- the concept ------------------------------------------------------
    y = section(draw, y + 26, "The idea")
    y = bullets(draw, y, [
        [("Accounts stop being silos.", "b"),
         (" One folder tree covers all of them, so a single folder can hold "
          "files that physically live on three different accounts.", "r")],
        [("A file goes whole to one account", "b"),
         (" — whichever has the most room free. Nothing is cut up "
          "needlessly, and Drive's own preview keeps working.", "r")],
        [("Only a file too big for any single account is split", "b"),
         (" — into 50 MB pieces, spread across several:", "r")],
    ])

    y = diagram(draw, y + 24)

    # --- what you do ------------------------------------------------------
    y = section(draw, y + 28, "What you do")
    y = steps(draw, y, [
        [("Sign in", "b"), (" with every Google account you own.", "r")],
        [("Upload a file.", "b"),
         (" It lands whole on whichever account has the most room free.", "r")],
        [("Download it back", "b"),
         (" — Gstore fetches it and rebuilds a split file.", "r")],
    ])

    # --- what is in it ----------------------------------------------------
    y = section(draw, y + 28, "What is in it")
    y = bullets(draw, y, [
        [("One pooled total", "b"),
         (" across every account, with a bar for each.", "r")],
        [("List, Grid and Gallery", "b"),
         (" views, with real Drive thumbnails.", "r")],
        [("Scan Drive", "b"),
         (" lists every file across all your accounts, live.", "r")],
    ])

    # --- the things people ask --------------------------------------------
    y = section(draw, y + 24, "Good to know")
    y = bullets(draw, y, [
        [("No Gstore server, no Gstore account.", "b"),
         (" Bytes go straight to Google.", "r")],
        [("Checked end to end", "b"),
         (" — a download lands only once its checksum matches.", "r")],
        [("Sign-in tokens are encrypted", "b"),
         (", the key kept in your OS keychain.", "r")],
    ])

    # --- where to get it --------------------------------------------------
    y = max(y + 28, H - 148)
    draw.line([M, y, W - M, y], fill=RULE, width=2)
    y += 28
    draw.text((M, y), "gstore.iampkb.dev", font=font(42, "Semibold"),
              fill=ACCENT)
    draw.text((M, y + 60), "Free download · macOS, Windows, Linux · "
              "no sign-up with us", font=font(24), fill=MUTED)

    if (bottom := y + 60 + 32) > H - 22:
        print(f"  warning: content runs to y={bottom}, past the {H}px canvas")

    path = OUT / f"release-{__version__}-story.png"
    img.save(path)
    print(f"wrote {path.relative_to(ROOT)}  {img.width}x{img.height}")


if __name__ == "__main__":
    main()
