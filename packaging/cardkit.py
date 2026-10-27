"""Text layout shared by the social-card renderers.

make_poster.py and make_release_card.py are independent designs -- light
story card, dark release-notes card -- but they wrap text the same way, and
the wrapping has one subtlety worth keeping in a single place: lines break on
words, not on style runs. Breaking per run put a space before every comma and
full stop that happened to follow a bold phrase ("50 MB pieces , spread"), so
a word here is a list of (text, style) segments and may change weight
mid-word.

Styles are single letters, and each caller supplies its own palette:

    "r"  regular      "b"  bold/emphasis
    "m"  muted        "l"  link (underlined)
"""

from __future__ import annotations

from PIL import ImageFont

FONT = "/System/Library/Fonts/SFNS.ttf"

_WEIGHTS = {"r": "Regular", "b": "Semibold", "m": "Regular", "l": "Regular"}


def font(size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(FONT, size)
    try:
        f.set_variation_by_name(weight)
    except Exception:
        pass                    # a non-variable face: the base weight is fine
    return f


def faces(size: int) -> dict:
    return {st: font(size, w) for st, w in _WEIGHTS.items()}


def words(runs):
    """Split runs into words, each a list of (text, style) segments."""
    out, cur = [], []
    for text, style in runs:
        for ch in text:
            if ch == " ":
                if cur:
                    out.append(cur)
                    cur = []
            elif cur and cur[-1][1] == style:
                cur[-1] = (cur[-1][0] + ch, style)
            else:
                cur.append((ch, style))
    if cur:
        out.append(cur)
    return out


def layout(draw, runs, size, max_width):
    """Wrap into lines of (word, width); returns (lines, line_height)."""
    f = faces(size)
    space = draw.textlength(" ", font=f["r"])
    lines, line, width = [], [], 0.0
    for word in words(runs):
        w = sum(draw.textlength(t, font=f[st]) for t, st in word)
        step = w if not line else space + w
        if line and width + step > max_width:
            lines.append(line)
            line, width = [(word, w)], w
        else:
            line.append((word, w))
            width += step
    if line:
        lines.append(line)
    return lines, draw.textbbox((0, 0), "Hgy", font=f["r"])[3]


def paint(draw, x, y, lines, line_h, size, fills, leading=14):
    f = faces(size)
    space = draw.textlength(" ", font=f["r"])
    under = line_h + 5
    for ln in lines:
        cx, prev = x, None
        for word, _ in ln:
            if prev is not None:
                if prev == "l" and word[0][1] == "l":   # the link's own spaces
                    draw.line([cx, y + under, cx + space, y + under],
                              fill=fills["l"], width=2)
                cx += space
            for text, st in word:
                w = draw.textlength(text, font=f[st])
                draw.text((cx, y), text, font=f[st], fill=fills[st])
                if st == "l":
                    draw.line([cx, y + under, cx + w, y + under],
                              fill=fills["l"], width=2)
                cx += w
            prev = word[-1][1]
        y += line_h + leading
    return y


def rich(draw, x, y, runs, size, max_width, fills, leading=14) -> int:
    """Draw a paragraph of mixed-weight text; returns the y below it."""
    lines, line_h = layout(draw, runs, size, max_width)
    return paint(draw, x, y, lines, line_h, size, fills, leading)


def height(draw, runs, size, max_width, leading=14) -> int:
    """What rich() would consume, without drawing it."""
    lines, line_h = layout(draw, runs, size, max_width)
    return len(lines) * (line_h + leading)
