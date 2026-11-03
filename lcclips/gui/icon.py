"""The LCClips icon (rounded accent square + record button), drawn with Pillow.
Used for the window/tray icon at runtime and for the .exe/installer icon at build time."""
from __future__ import annotations

from pathlib import Path

DEFAULT_ACCENT = "#e8720c"
REC_RED = "#e5484d"


def make_icon(size: int = 256, accent: str = DEFAULT_ACCENT):
    from PIL import Image, ImageDraw

    # Draw at 4x and downsample for smooth edges at small sizes.
    big = size * 4
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = big / 64
    d.rounded_rectangle((2 * s, 2 * s, 62 * s, 62 * s), radius=14 * s, fill=accent)
    d.ellipse((17 * s, 17 * s, 47 * s, 47 * s), fill="#141414")
    d.ellipse((25 * s, 25 * s, 39 * s, 39 * s), fill=REC_RED)
    return img.resize((size, size), Image.LANCZOS)


def write_ico(path: Path, accent: str = DEFAULT_ACCENT) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    make_icon(256, accent).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path
