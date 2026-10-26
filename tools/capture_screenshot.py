#!/usr/bin/env python3
"""Capture a state from a real BUSY Bar and render it as an LED-matrix GIF for the README.

Draws the state the same way the plugin does (`busybar_status.py send`), reads the
front display's framebuffer back from `GET /api/screen` for one animation loop, and
renders the frames as glowing LED pixels inside a small device bezel.

Needs Pillow (the plugin itself is standard-library only):

    python3 tools/capture_screenshot.py --url 10.0.4.20 --state WORKING --activity Editing \\
        --out docs/working.gif
"""

from __future__ import annotations

import argparse
import base64
import sys
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import busybar_status as status  # noqa: E402

WIDTH, HEIGHT = 72, 16
CELL = 10  # output pixels per LED
LED = 8  # lit square size inside the cell
MARGIN = 18


def capture(client: status.BusyBarClient, seconds: float, interval: float):
    frames, deadline, start = [], None, time.monotonic()
    index = 0
    while True:
        due = start + index * interval
        time.sleep(max(0.0, due - time.monotonic()))
        raw = base64.b64decode(client.request("GET", "/screen", {"display": "0"}))
        if len(raw) != WIDTH * HEIGHT * 3:
            raise RuntimeError("unexpected framebuffer size %d" % len(raw))
        frames.append([(raw[i + 2], raw[i + 1], raw[i]) for i in range(0, len(raw), 3)])  # device sends BGR
        index += 1
        if time.monotonic() - start >= seconds:
            return frames


def render(pixels) -> Image.Image:
    width, height = WIDTH * CELL + 2 * MARGIN, HEIGHT * CELL + 2 * MARGIN
    body = Image.new("RGB", (width, height), (24, 25, 30))
    draw = ImageDraw.Draw(body)
    draw.rounded_rectangle((1, 1, width - 2, height - 2), radius=14, outline=(52, 54, 62), width=2)
    draw.rounded_rectangle((MARGIN - 6, MARGIN - 6, width - MARGIN + 5, height - MARGIN + 5), radius=6, fill=(5, 5, 7))
    lit = Image.new("RGB", (width, height), (0, 0, 0))
    lit_draw = ImageDraw.Draw(lit)
    pad = (CELL - LED) // 2
    for y in range(HEIGHT):
        for x in range(WIDTH):
            r, g, b = pixels[y * WIDTH + x]
            x0, y0 = MARGIN + x * CELL + pad, MARGIN + y * CELL + pad
            box = (x0, y0, x0 + LED - 1, y0 + LED - 1)
            draw.rounded_rectangle(box, radius=2, fill=(16, 16, 20))  # LED that is off
            if r or g or b:
                lit_draw.rounded_rectangle(box, radius=2, fill=(min(255, r + 14), min(255, g + 14), min(255, b + 14)))
    glow = lit.filter(ImageFilter.GaussianBlur(5)).point(lambda v: int(v * 0.55))
    image = ImageChops.add(body, glow)
    image.paste(lit, mask=lit.convert("L").point(lambda v: 255 if v else 0))
    return image


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="device address or API base URL (default: plugin configuration)")
    parser.add_argument("--state", default="WORKING", choices=status.STATUSES)
    parser.add_argument("--activity", default="", help="subtitle to show (WORKING)")
    parser.add_argument("--seconds", type=float, default=2.0, help="how much animation to record")
    parser.add_argument("--fps", type=float, default=25.0)
    parser.add_argument("--out", default=str(ROOT / "docs" / "working.gif"))
    args = parser.parse_args()

    config = status.Config.from_environment()
    if args.url:
        import dataclasses

        config = dataclasses.replace(config, base_url=status.normalize_base_url(args.url))
    client = status.BusyBarClient(config)
    plan = status.asset_plan(config, args.state)
    assets = status.resolve_assets(config, None, client, plan)
    client.send(args.state, activity=args.activity, assets=assets)
    status.forget_drawn(config)  # the next real hook redraws from scratch
    time.sleep(0.15)  # let the animations start

    frames = capture(client, args.seconds, 1.0 / args.fps)
    images = [render(frame) for frame in frames]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    palette = images[0].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    quantized = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
    quantized[0].save(
        out, save_all=True, append_images=quantized[1:], duration=int(1000 / args.fps), loop=0, optimize=True, disposal=1
    )
    print("wrote %s (%d frames, %.0f KB)" % (out, len(frames), out.stat().st_size / 1024))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
