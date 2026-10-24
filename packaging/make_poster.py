"""Render a 9:16 story poster announcing a release.

    .venv/bin/python packaging/make_poster.py

Writes assets/social/release-<version>-story.png at 1080x1920, the size
Instagram, WhatsApp and Threads stories expect. Everything is drawn here
rather than exported from a design tool so the next release is one command
away rather than an afternoon.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from gstore import __version__

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "social"
W, H = 1080, 1920

FONT = "/System/Library/Fonts/SFNS.ttf"

INK = (255, 255, 255)
DIM = (150, 163, 178)
BG = (9, 12, 17)
BLUE = (58, 150, 255)
GREEN = (72, 214, 160)


def font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(FONT, size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass  # a non-variable fallback still renders, just at one weight
    return f


def text_width(draw, s: str, f) -> int:
    return draw.textbbox((0, 0), s, font=f)[2]


def centred(draw, y: int, s: str, f, fill=INK) -> int:
    """Draw `s` centred on the canvas; return the y below it."""
    box = draw.textbbox((0, 0), s, font=f)
    draw.text(((W - box[2]) / 2, y), s, font=f, fill=fill)
    return y + (box[3] - box[1]) + box[1]


def backdrop() -> Image.Image:
    """Dark ground with a blue-to-green glow behind the mark."""
    img = Image.new("RGB", (W, H), BG)
    px = img.load()
    cx, cy, radius = W / 2, 560, 980
    for y in range(H):
        for x in range(0, W, 2):          # every other column, then smoothed
            d = math.hypot(x - cx, y - cy) / radius
            if d >= 1:
                continue
            t = (1 - d) ** 2.4 * 0.55
            mix = min(1.0, max(0.0, (x / W) * 0.65 + (y / H) * 0.35))
            r = BG[0] + (BLUE[0] * (1 - mix) + GREEN[0] * mix - BG[0]) * t
            g = BG[1] + (BLUE[1] * (1 - mix) + GREEN[1] * mix - BG[1]) * t
            b = BG[2] + (BLUE[2] * (1 - mix) + GREEN[2] * mix - BG[2]) * t
            px[x, y] = (int(r), int(g), int(b))
            if x + 1 < W:
                px[x + 1, y] = (int(r), int(g), int(b))
    return img


def pool_diagram(draw, y: int) -> int:
    """Three account bars merging into one — the idea in one picture."""
    pad, gap = 110, 18
    width = W - pad * 2
    cell = (width - gap * 2) / 3

    label = font(30, "Medium")
    for i in range(3):
        x = pad + i * (cell + gap)
        draw.rounded_rectangle([x, y, x + cell, y + 96], radius=18,
                               fill=(26, 33, 44), outline=(48, 60, 76), width=2)
        s = "15 GB"
        draw.text((x + (cell - text_width(draw, s, label)) / 2, y + 32),
                  s, font=label, fill=DIM)

    arrow_y = y + 136
    draw.line([W / 2, arrow_y, W / 2, arrow_y + 44], fill=(70, 86, 106), width=3)
    draw.polygon([(W / 2 - 11, arrow_y + 44), (W / 2 + 11, arrow_y + 44),
                  (W / 2, arrow_y + 64)], fill=(70, 86, 106))

    bar_y = arrow_y + 92
    bar = Image.new("RGB", (int(width), 112))
    bp = bar.load()
    for x in range(bar.width):
        t = x / bar.width
        bp_col = (int(BLUE[0] + (GREEN[0] - BLUE[0]) * t),
                  int(BLUE[1] + (GREEN[1] - BLUE[1]) * t),
                  int(BLUE[2] + (GREEN[2] - BLUE[2]) * t))
        for yy in range(bar.height):
            bp[x, yy] = bp_col
    mask = Image.new("L", bar.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bar.width - 1, bar.height - 1],
                                           radius=22, fill=255)
    draw._image.paste(bar, (pad, bar_y), mask)

    big = font(46, "Bold")
    s = "45 GB, one drive"
    draw.text(((W - text_width(draw, s, big)) / 2, bar_y + 31),
              s, font=big, fill=(10, 16, 24))
    return bar_y + 112


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    img = backdrop()
    draw = ImageDraw.Draw(img)
    draw._image = img  # pool_diagram pastes through this

    # mark
    logo = Image.open(ROOT / "assets" / "gstore_logo_512.png").convert("RGBA")
    logo = logo.resize((232, 232), Image.LANCZOS)
    img.paste(logo, ((W - 232) // 2, 168), logo)

    y = 452
    y = centred(draw, y, "Gstore", font(104, "Bold")) + 18

    badge = f"version {__version__}"
    bf = font(30, "Semibold")
    bw = text_width(draw, badge, bf)
    draw.rounded_rectangle([(W - bw) / 2 - 26, y - 6, (W + bw) / 2 + 26, y + 52],
                           radius=29, fill=(26, 33, 44), outline=(52, 66, 84), width=2)
    draw.text(((W - bw) / 2, y + 10), badge, font=bf, fill=(150, 200, 255))
    y += 104

    y = centred(draw, y, "Pool the free space of several", font(44, "Regular"), DIM)
    y = centred(draw, y + 14, "Google accounts into one drive", font(44, "Regular"), DIM)

    y = pool_diagram(draw, y + 88) + 118

    # Three short lines, not three sentences. A story is scrolled past, not
    # read, so each point has to land in one glance.
    points = [
        "Splits big files across accounts",
        "Resumes interrupted transfers",
        "Runs entirely on your machine",
    ]
    body = font(44, "Regular")
    dot = font(44, "Bold")
    for line in points:
        w = text_width(draw, line, body) + 38
        x = (W - w) / 2
        draw.text((x, y), "\u2022", font=dot, fill=GREEN)
        draw.text((x + 38, y), line, font=body, fill=(214, 223, 234))
        y += 76

    # Footer flows below the content rather than sitting at a fixed offset —
    # a hardcoded y collided with the body on the first attempt.
    y = max(y + 72, H - 250)
    y = centred(draw, y, "macOS  \u00b7  Windows  \u00b7  Linux",
                font(32, "Medium"), DIM) + 46
    y = centred(draw, y, "gstore.iampkb.dev", font(48, "Semibold"), (120, 200, 255))
    if y > H - 40:
        print(f"warning: content runs to y={y}, past the {H}px canvas")

    path = OUT / f"release-{__version__}-story.png"
    img.save(path)
    print(f"wrote {path.relative_to(ROOT)}  {img.width}x{img.height}")


if __name__ == "__main__":
    main()
