"""Render the release-notes card in landscape and portrait.

    .venv/bin/python packaging/make_release_card.py

Writes, for version <v>:

    assets/social/release-<v>-notes-landscape.png   1920x1080
    assets/social/release-<v>-notes-portrait.png    1080x1920

A separate design from make_poster.py, which keeps its own story card; both
are kept so there is a choice at posting time.

The look follows a release-notes page rather than a poster: dark ground,
wordmark top left, "<v> Release Notes" as the heading, a link line, then
prose. Emphasis is carried by weight and colour inside the running text --
the way a notes page bolds the terms that matter -- which is why there is an
inline run renderer, shared with make_poster.py in cardkit. The only
ornament is the app icon.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cardkit as ck                                        # noqa: E402
from cardkit import font                                    # noqa: E402

from gstore import __version__                              # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "social"

RELEASED = "24 October 2026"
PREVIOUS = "0.0.4"
COMMITS = 36        # git rev-list --count v0.0.4..v0.1.0
TESTS = 190         # pytest --collect-only
SPAN = "five days"  # 19 to 24 October 2026

BG = (26, 26, 26)
TEXT = (203, 208, 214)
BRIGHT = (244, 246, 248)
MUTED = (138, 145, 153)
LINK = (141, 152, 246)
RULE = (58, 60, 63)

HIGHLIGHTS = [
    [("Parallel transfers.", "b"),
     (" Pieces move several at a time — 4.3× faster downloads, "
      "2.5× faster uploads.", "r")],
    [("Transfers resume.", "b"),
     (" An interrupted upload or download carries on from the piece that "
      "was in flight.", "r")],
    [("Dropped connections survive.", "b"),
     (" A transfer waits and retries instead of ending.", "r")],
    [("Live progress.", "b"),
     (" Speed, ETA and the piece in flight, while it runs.", "r")],
    [("Linux builds,", "b"),
     (" and a notice inside the app when a new version is out.", "r")],
]

FILLS = {"r": TEXT, "b": BRIGHT, "m": MUTED, "l": LINK}


def rich(draw, x, y, runs, size, max_width, leading=14) -> int:
    return ck.rich(draw, x, y, runs, size, max_width, FILLS, leading)


def masthead(img, draw, x, y, logo_px, word_px) -> int:
    logo = Image.open(ROOT / "assets" / "gstore_logo_512.png").convert("RGBA")
    logo = logo.resize((logo_px, logo_px), Image.LANCZOS)
    img.paste(logo, (x, y), logo)
    f = font(word_px, "Bold")
    top = draw.textbbox((0, 0), "GSTORE", font=f)[1]
    draw.text((x + logo_px + int(logo_px * 0.26),
               y - top + (logo_px - word_px) // 2),
              "GSTORE", font=f, fill=BRIGHT)
    return y + logo_px


def item_height(draw, runs, size, width, leading=8) -> int:
    return ck.height(draw, runs, size, width - 32, leading)


def balanced_split(draw, items, size, width) -> int:
    """Where to cut the list so the two columns end at about the same depth.

    A fixed 3/2 cut left the tall items stacked in one column and ran the
    card off the bottom; this measures them first.
    """
    h = [item_height(draw, runs, size, width) for runs in items]
    return min(range(1, len(h)),
               key=lambda k: abs(sum(h[:k]) - sum(h[k:])))


def highlight_column(draw, x, y, items, size, width, gap) -> int:
    for runs in items:
        draw.line([x, y + size * 0.62, x + 16, y + size * 0.62],
                  fill=MUTED, width=3)
        y = rich(draw, x + 32, y, runs, size, width - 32, leading=8) + gap
    return y - gap


def render(landscape: bool) -> Path:
    W, H = (1920, 1080) if landscape else (1080, 1920)
    M = 76 if landscape else 80
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    full = W - 2 * M
    # Landscape reserves the top right for the icon, then runs the highlights
    # across the whole width in two columns; portrait is one column throughout.
    col = 1230 if landscape else full
    body = 30 if landscape else 33
    lead = 26 if landscape else 31

    y = masthead(img, draw, M, 48, 120 if landscape else 116,
                 92 if landscape else 90)

    y += 34
    draw.text((M, y), f"{__version__} Release Notes",
              font=font(62 if landscape else 60, "Bold"), fill=BRIGHT)
    y += 86

    y = rich(draw, M, y, [("gstore.iampkb.dev/release-notes", "l")],
             33, col, leading=0)

    y += 30
    y = rich(draw, M, y, [
        ("Gstore lays ", "r"),
        ("one folder tree", "b"),
        (" over several Google accounts, so their free storage works as a "
         "single pool. A file too big for any single account is split "
         "into ", "r"),
        ("50 MB pieces", "b"),
        (", spread across every account you have linked, and rebuilt ", "r"),
        ("byte for byte", "b"),
        (" when you download it.", "r"),
    ], body, col)

    y += 18
    y = rich(draw, M, y, [
        ("Files move straight between your computer and Google — ", "r"),
        ("nothing passes through a server of ours", "b"),
        (". Free to use, on macOS, Windows and Linux.", "r"),
    ], body, col)

    y += 18
    y = rich(draw, M, y, [
        (f"This release is {SPAN} of work: ", "r"),
        (f"{COMMITS} commits", "b"),
        (f" since {PREVIOUS}, under ", "r"),
        (f"{TESTS} tests", "b"),
        (".", "r"),
    ], body, col)

    # the icon, placed where each layout actually has room
    art = Image.open(ROOT / "assets" / "gstore_logo_1024.png").convert("RGBA")
    if landscape:
        size = 380
        art = art.resize((size, size), Image.LANCZOS)
        img.paste(art, (W - M - size, 210), art)

    y += 26
    draw.line([M, y, W - M, y], fill=RULE, width=2)
    y += 22
    draw.text((M, y), "HIGHLIGHTS", font=font(25, "Semibold"), fill=MUTED)
    y += 38

    if landscape:
        gutter = 72
        cw = (full - gutter) // 2
        split = balanced_split(draw, HIGHLIGHTS, lead, cw)
        left = highlight_column(draw, M, y, HIGHLIGHTS[:split], lead, cw, 20)
        right = highlight_column(draw, M + cw + gutter, y, HIGHLIGHTS[split:],
                                 lead, cw, 20)
        y = max(left, right)
    else:
        y = highlight_column(draw, M, y, HIGHLIGHTS, lead, full, 26)
        room = (H - 100) - y
        if room > 300:
            size = min(380, room - 80)
            art = art.resize((size, size), Image.LANCZOS)
            img.paste(art, ((W - size) // 2, y + (room - size) // 2), art)

    foot = font(26)
    draw.text((M, H - 72), f"Released {RELEASED}", font=foot, fill=MUTED)
    label = "github.com/Parthivsurya/Gstorage"
    draw.text((W - M - draw.textlength(label, font=foot), H - 72), label,
              font=foot, fill=MUTED)

    if y > H - 108:
        print(f"  warning: content runs to y={int(y)}, past {H - 108}")

    name = "landscape" if landscape else "portrait"
    path = OUT / f"release-{__version__}-notes-{name}.png"
    img.save(path)
    print(f"wrote {path.relative_to(ROOT)}  {W}x{H}")
    return path


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    render(landscape=True)
    render(landscape=False)


if __name__ == "__main__":
    main()
