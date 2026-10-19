"""Generate the Gstore app logo.

Draws a rounded-square mark: a blue→green vertical gradient (evoking Google
Drive), stacked "layers" implying multiple drives pooled into one, and a bold
white "G". Rendered at high resolution then downscaled for crisp edges.

    .venv/bin/python packaging/make_logo.py

Outputs assets/gstore_logo_1024.png (master, reusable as the .app icon) and
assets/gstore_logo_120.png (Google OAuth consent screen: square, <=1 MB).
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

SS = 1024  # master / supersample size

TOP = (66, 133, 244)     # Google blue
BOTTOM = (52, 168, 83)   # Google green


def _gradient(size: int) -> Image.Image:
    """Vertical TOP→BOTTOM gradient as an RGBA image."""
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        grad.putpixel(
            (0, y),
            tuple(round(TOP[i] + (BOTTOM[i] - TOP[i]) * t) for i in range(3)),
        )
    return grad.resize((size, size)).convert("RGBA")


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255
    )
    return mask


def _load_font(px: int) -> ImageFont.FreeTypeFont:
    for path in (
        "/System/Library/Fonts/SFNSRounded.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ):
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                continue
    return ImageFont.load_default()


def build() -> Image.Image:
    icon = _gradient(SS)

    draw = ImageDraw.Draw(icon)
    # Faint stacked "drive layers" behind the G — the aggregation motif.
    layer_w = int(SS * 0.46)
    layer_h = int(SS * 0.10)
    cx = SS // 2
    for i, alpha in enumerate((70, 110)):
        y = int(SS * 0.30) + i * int(layer_h * 1.5)
        overlay = Image.new("RGBA", icon.size, (0, 0, 0, 0))
        ImageDraw.Draw(overlay).rounded_rectangle(
            [cx - layer_w // 2, y, cx + layer_w // 2, y + layer_h],
            radius=layer_h // 2,
            fill=(255, 255, 255, alpha),
        )
        icon = Image.alpha_composite(icon, overlay)

    draw = ImageDraw.Draw(icon)
    font = _load_font(int(SS * 0.62))
    text = "G"
    box = draw.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    draw.text(
        ((SS - tw) / 2 - box[0], (SS - th) / 2 - box[1] + int(SS * 0.04)),
        text,
        font=font,
        fill=(255, 255, 255, 255),
    )

    # Clip to a rounded square.
    icon.putalpha(_rounded_mask(SS, int(SS * 0.22)))
    return icon


LEGACY_NOTICE = """\
make_logo.py is superseded. The logo is now authored artwork, kept at
assets/gstore_logo_source.png, and assets/gstore_logo_1024.png is derived from
it. This script drew the earlier programmatic mark and would overwrite both.

Kept for reference only. Re-run deliberately with --force if you really want
to regenerate the old drawn logo.
"""


def main() -> None:
    import sys

    if "--force" not in sys.argv:
        sys.exit(LEGACY_NOTICE)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "assets")
    os.makedirs(out_dir, exist_ok=True)

    master = build()
    master.save(os.path.join(out_dir, "gstore_logo_1024.png"))
    master.resize((120, 120), Image.LANCZOS).save(
        os.path.join(out_dir, "gstore_logo_120.png")
    )
    print("wrote assets/gstore_logo_1024.png and assets/gstore_logo_120.png")


if __name__ == "__main__":
    main()
