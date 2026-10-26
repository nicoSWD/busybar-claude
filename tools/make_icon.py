#!/usr/bin/env python3
"""Render the plugin icon (.claude-plugin/icon.png) from the READY mascot. Needs Pillow."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import busybar_mascot as mascot  # noqa: E402

SIZE = 512
SCALE = 24  # 16 sprite pixels * 24 = 384, leaving a 64px margin


def main() -> None:
    frame = mascot.ready()[14]
    image = Image.new("RGB", (SIZE, SIZE), (22, 23, 28))
    draw = ImageDraw.Draw(image)
    offset = (SIZE - mascot.SIZE * SCALE) // 2
    for y in range(mascot.SIZE):
        for x in range(mascot.SIZE):
            pixel = frame.pixels[y * mascot.SIZE + x]
            if pixel:
                x0, y0 = offset + x * SCALE, offset + y * SCALE
                draw.rectangle((x0, y0, x0 + SCALE - 1, y0 + SCALE - 1), fill=pixel)
    out = ROOT / ".claude-plugin" / "icon.png"
    image.save(out)
    print("wrote %s" % out)


if __name__ == "__main__":
    main()
