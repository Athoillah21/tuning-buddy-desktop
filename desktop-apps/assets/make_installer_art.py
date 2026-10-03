"""
Draw the artwork for the engine's own wizard (shown only when WebView2 is missing): a tall side
image and a small corner mark, in light and dark, matching the custom setup window.
Writes build/art/*.png. Run by build.ps1.
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parent.parent / "build" / "art"

THEMES = {
    "": {"bg": ((236, 242, 255), (247, 249, 253)), "blobs": [((0, 102, 255), 70), ((124, 92, 255), 50), ((0, 194, 255), 45)],
         "text": (11, 18, 32), "sub": (74, 84, 102), "tile": ((42, 125, 255), (0, 82, 214))},
    "-dark": {"bg": ((14, 19, 32), (10, 13, 20)), "blobs": [((37, 99, 235), 110), ((124, 58, 237), 70), ((6, 182, 212), 45)],
              "text": (238, 241, 247), "sub": (164, 173, 189), "tile": ((75, 141, 255), (29, 95, 224))},
}


def font(size: int, bold: bool = False):
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"), "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def gradient(size, top, bottom):
    w, h = size
    image = Image.new("RGB", size, top)
    d = ImageDraw.Draw(image)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return image


def blobs(image, spots, theme):
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for (x, y, r), (color, alpha) in zip(spots, theme["blobs"]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=(*color, alpha))
    layer = layer.filter(ImageFilter.GaussianBlur(min(image.size) * 0.18))
    return Image.alpha_composite(image.convert("RGBA"), layer)


def logo(size: int, theme) -> Image.Image:
    """The app icon: a rounded gradient tile with a gauge whose needle points at 'fast'."""
    s = size * 4
    tile = gradient((s, s), *theme["tile"]).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.23), fill=255)
    tile.putalpha(mask)
    d = ImageDraw.Draw(tile)
    white = (255, 255, 255, 255)
    cx, cy, r = s / 2, s * 0.56, s * 0.30
    width = int(s * 0.07)
    d.arc((cx - r, cy - r, cx + r, cy + r), start=150, end=390, fill=white, width=width)
    angle = math.radians(-35)
    d.line((cx, cy, cx + r * 0.78 * math.cos(angle), cy + r * 0.78 * math.sin(angle)), fill=white, width=int(width * 0.9))
    hub = s * 0.056
    d.ellipse((cx - hub, cy - hub, cx + hub, cy + hub), fill=white)
    return tile.resize((size, size), Image.LANCZOS)


def glow(image, center, radius, color, alpha):
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    x, y = center
    ImageDraw.Draw(layer).ellipse((x - radius, y - radius, x + radius, y + radius), fill=(*color, alpha))
    return Image.alpha_composite(image, layer.filter(ImageFilter.GaussianBlur(radius * 0.6)))


def side(theme) -> Image.Image:
    w, h = 328, 628  # 2x the modern wizard's 164x314; Inno scales it down crisply
    image = gradient((w, h), *theme["bg"])
    image = blobs(image, [(40, 90, 200), (300, 330, 170), (120, 600, 180)], theme)
    image = glow(image, (w // 2, 210), 90, theme["tile"][0], 120)
    mark = logo(124, theme)
    image.alpha_composite(mark, (w // 2 - 62, 148))
    d = ImageDraw.Draw(image)
    title = "Tuning Buddy"
    f = font(34, bold=True)
    tw = d.textlength(title, font=f)
    d.text(((w - tw) / 2, 300), title, font=f, fill=theme["text"])
    sub = font(19)
    for i, line in enumerate(("Make slow PostgreSQL", "queries fast")):
        lw = d.textlength(line, font=sub)
        d.text(((w - lw) / 2, 352 + i * 26), line, font=sub, fill=theme["sub"])
    return image.convert("RGB")


def small(theme) -> Image.Image:
    size = 110  # 2x of 55x55
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    image.alpha_composite(logo(92, theme), (9, 9))
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for suffix, theme in THEMES.items():
        side(theme).save(OUT / f"wizard-side{suffix}.png")
        small(theme).save(OUT / f"wizard-small{suffix}.png")
    print(f"Wrote installer art to {OUT}")


if __name__ == "__main__":
    sys.exit(main())
