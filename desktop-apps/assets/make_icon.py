"""
Draw assets/icon.ico: a rounded blue tile with a white gauge needle (query tuning).
Colors follow the app's --accent (#0066ff). Run by build.ps1 when the icon is missing.
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
ACCENT = (0, 102, 255, 255)
WHITE = (255, 255, 255, 255)
OUT = Path(__file__).resolve().parent / "icon.ico"


def draw() -> Image.Image:
    scale = 4  # supersample, then downsize for smooth edges
    s = SIZE * scale
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=ACCENT)

    # Gauge arc (upper 240 degrees) and a needle pointing to "fast"
    cx, cy, r = s / 2, s * 0.56, s * 0.30
    width = int(s * 0.07)
    d.arc((cx - r, cy - r, cx + r, cy + r), start=150, end=390, fill=WHITE, width=width)
    angle = math.radians(-35)
    tip = (cx + r * 0.78 * math.cos(angle), cy + r * 0.78 * math.sin(angle))
    d.line((cx, cy, *tip), fill=WHITE, width=int(width * 0.9))
    hub = s * 0.055
    d.ellipse((cx - hub, cy - hub, cx + hub, cy + hub), fill=WHITE)

    return image.resize((SIZE, SIZE), Image.LANCZOS)


if __name__ == "__main__":
    draw().save(OUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"Wrote {OUT}")
