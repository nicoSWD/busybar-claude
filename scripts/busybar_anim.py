"""BUSY Bar `.anim` encoder and the WORKING progress animation.

The container layout follows the firmware's `lib/anim_file/anim_file_format.h`
(as implemented by the `busybar-anim` crate): a 36-byte header, one section
record, then one record per frame, RLE-compressed when that is smaller. Only the
BGR888 pixel layout used by the 72x16 front display is supported.

The animation is generated at runtime from the configured WORKING color, so no
binary asset ships with the plugin and the color setting is honored.
"""

from __future__ import annotations

import colorsys
import hashlib
import math
import struct
from typing import List, Sequence, Tuple

ANIM_SIGNATURE = b"bicycle0"
ANIM_HEADER_LEN = 36
ANIM_SECTION = b"default"
ANIM_PIXEL_LAYOUT_BGR888 = 0
ANIM_BLOCK_SIZE = 3
ANIM_RLE_MAX_BLOCKS = 127
ANIM_RLE_RUN_THRESHOLD = 3

# Bump when the artwork changes so devices get a fresh asset name.
ANIMATION_VERSION = 2
ANIMATION_WIDTH = 72
ANIMATION_HEIGHT = 2
ANIMATION_FPS = 24
ANIMATION_FRAMES = 48  # one full there-and-back sweep, 2 seconds
ANIMATION_RAIL = 0.10  # brightness of the idle track the comet travels along
ANIMATION_ROW_GAIN = (0.55, 1.0)  # top row is a softer glow above the bright row
ANIMATION_SIGMA = 2.2  # comet head radius in pixels
ANIMATION_TAIL = 0.82  # per-frame decay of the trail
ANIMATION_TRAIL_FRAMES = 14

Rgb = Tuple[int, int, int]


def rle_compress(data: bytes, block_size: int = ANIM_BLOCK_SIZE) -> bytes:
    """Byte-for-byte port of the firmware's `rle_encode` scanner."""
    count = len(data) // block_size

    def block(index: int) -> bytes:
        return data[index * block_size:(index + 1) * block_size]

    out = bytearray()
    i = 0
    while i < count:
        run = 1
        while i + run < count and run < ANIM_RLE_MAX_BLOCKS and block(i + run) == block(i):
            run += 1
        if run >= ANIM_RLE_RUN_THRESHOLD:
            out.append(run)
            out += block(i)
            i += run
            continue
        verbatim = pending = 0
        for j in range(i, count):
            if j + 1 < count and block(j + 1) == block(j):
                pending += 1
                if pending > ANIM_RLE_RUN_THRESHOLD:
                    break
            else:
                verbatim += 1 + pending
                pending = 0
        verbatim = min(verbatim + pending, ANIM_RLE_MAX_BLOCKS)
        out.append(0x80 | verbatim)
        out += data[i * block_size:(i + verbatim) * block_size]
        i += verbatim
    return bytes(out)


def rle_decompress(data: bytes, block_size: int = ANIM_BLOCK_SIZE) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        opcode = data[i]
        count = opcode & 0x7F
        i += 1
        if count == 0:
            raise ValueError("zero-length RLE opcode")
        if opcode & 0x80:
            end = i + count * block_size
            if end > len(data):
                raise ValueError("truncated literal")
            out += data[i:end]
            i = end
        else:
            if i + block_size > len(data):
                raise ValueError("truncated run")
            out += data[i:i + block_size] * count
            i += block_size
    return bytes(out)


def encode_anim(width: int, height: int, fps: int, frames: Sequence[bytes]) -> bytes:
    """Encode BGR888 frames (each held for one display frame) as a `.anim` file."""
    if not frames:
        raise ValueError("an animation needs at least one frame")
    expected = width * height * ANIM_BLOCK_SIZE
    records = bytearray()
    max_encoded = 0
    for index, pixels in enumerate(frames):
        if len(pixels) != expected:
            raise ValueError("frame %d is %d bytes, expected %d" % (index, len(pixels), expected))
        compressed = rle_compress(pixels)
        encoding, payload = (1, compressed) if len(compressed) < len(pixels) else (0, pixels)
        max_encoded = max(max_encoded, len(payload))
        records += bytes([encoding, 1]) + struct.pack("<H", len(payload)) + payload
    section_len = 4 + 4 + 4 + 1 + len(ANIM_SECTION) + 1
    count = len(frames)
    header = bytearray(ANIM_SIGNATURE)
    header += bytes([0, width, height, ANIM_PIXEL_LAYOUT_BGR888, fps])
    header += struct.pack("<H", max_encoded) + bytes([0])
    header += struct.pack("<IIIII", section_len, len(records), 1, count, count)
    section = struct.pack("<III", 0, count - 1, ANIM_HEADER_LEN + section_len)
    section += bytes([1]) + ANIM_SECTION + b"\0"
    return bytes(header) + section + bytes(records)


def comet_frames(color: Rgb, width: int = ANIMATION_WIDTH, height: int = ANIMATION_HEIGHT,
                 frames: int = ANIMATION_FRAMES) -> List[bytes]:
    """A glowing comet that eases back and forth along a dim rail, leaving a fading trail.

    The head follows a cosine so it decelerates into each turn; the trail is the
    max of the head's previous positions, which keeps it continuous through the
    turnarounds and makes the loop seamless.
    """

    def position(frame: int) -> float:
        return (width - 1) * (0.5 - 0.5 * math.cos(2 * math.pi * frame / frames))

    out: List[bytes] = []
    for frame in range(frames):
        intensity = [0.0] * width
        for age in range(ANIMATION_TRAIL_FRAMES):
            center = position((frame - age) % frames)
            weight = ANIMATION_TAIL ** age
            for x in range(width):
                value = math.exp(-((x - center) ** 2) / (2 * ANIMATION_SIGMA ** 2)) * weight
                if value > intensity[x]:
                    intensity[x] = value
        rows = []
        for index in range(height):
            gain = ANIMATION_ROW_GAIN[min(index, len(ANIMATION_ROW_GAIN) - 1)]
            row = bytearray()
            for x in range(width):
                level = (ANIMATION_RAIL + (1.0 - ANIMATION_RAIL) * intensity[x]) * gain
                # The very core of the head glows toward white.
                white = max(0.0, intensity[x] - 0.6) / 0.4 * 0.5 * gain
                channels = [min(255, int(c * level + (255 - c * level) * white)) for c in color]
                row += bytes([channels[2], channels[1], channels[0]])  # BGR
            rows.append(bytes(row))
        out.append(b"".join(rows))
    return out


def parse_rgb(color: str) -> Rgb:
    """`#RRGGBBAA` (alpha ignored) to an RGB tuple."""
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)


def animation_asset_name(color: str, width: int = ANIMATION_WIDTH) -> str:
    material = "%d:%d:%d:%s" % (ANIMATION_VERSION, ANIMATION_FPS, width, color[1:7].upper())
    return "working-%s.anim" % hashlib.sha256(material.encode("ascii")).hexdigest()[:8]


def working_animation(color: str, width: int = ANIMATION_WIDTH) -> bytes:
    return encode_anim(width, ANIMATION_HEIGHT, ANIMATION_FPS, comet_frames(parse_rgb(color), width))


DOTS_WIDTH = 5  # three 1px dots with a 1px gap, like the "." glyph's spacing in the title font
DOTS_FPS = 3  # one step per ~0.33s: nothing, ., .., ... and around


def dots_frames(color: str) -> List[bytes]:
    r, g, b = parse_rgb(color)
    frames = []
    for lit in range(4):
        row = bytearray(DOTS_WIDTH * 3)
        for dot in range(lit):
            row[dot * 6:dot * 6 + 3] = bytes([b, g, r])  # BGR
        frames.append(bytes(row))
    return frames


def dots_asset_name(color: str) -> str:
    material = "%d:%d:%s" % (ANIMATION_VERSION, DOTS_FPS, color[1:7].upper())
    return "dots-%s.anim" % hashlib.sha256(material.encode("ascii")).hexdigest()[:8]


def dots_animation(color: str) -> bytes:
    """A 5x1 animation of dots appearing one by one, to follow a label as "WAITING...". """
    return encode_anim(DOTS_WIDTH, 1, DOTS_FPS, dots_frames(color))


PING_FRAMES = 30
PING_FPS = 15  # a two second cycle


def ping_frames(color: Rgb, width: int, height: int = ANIMATION_HEIGHT, frames: int = PING_FRAMES) -> List[bytes]:
    """Ripples spreading from the middle of a dim rail, one new ring every half cycle.

    Two rings are always in flight, each fading as it travels outward, so the pulse never
    stops; the cycle is periodic, so the loop is seamless. Used while Claude waits for you.
    """
    center = (width - 1) / 2.0
    out: List[bytes] = []
    for frame in range(frames):
        intensity = [0.0] * width
        for ring in range(2):
            progress = ((frame / frames) + ring * 0.5) % 1.0
            radius = progress * (center + 3)
            strength = (1.0 - progress) ** 0.8
            for x in range(width):
                value = math.exp(-((abs(x - center) - radius) ** 2) / (2 * 1.4 ** 2)) * strength
                if value > intensity[x]:
                    intensity[x] = value
        rows = []
        for index in range(height):
            gain = ANIMATION_ROW_GAIN[min(index, len(ANIMATION_ROW_GAIN) - 1)]
            row = bytearray()
            for x in range(width):
                level = (ANIMATION_RAIL + (1.0 - ANIMATION_RAIL) * intensity[x]) * gain
                white = max(0.0, intensity[x] - 0.7) / 0.3 * 0.4 * gain
                channels = [min(255, int(c * level + (255 - c * level) * white)) for c in color]
                row += bytes([channels[2], channels[1], channels[0]])  # BGR
            rows.append(bytes(row))
        out.append(b"".join(rows))
    return out


def ping_asset_name(color: str, width: int) -> str:
    material = "%d:%d:%d:%s" % (ANIMATION_VERSION, PING_FPS, width, color[1:7].upper())
    return "ping-%s.anim" % hashlib.sha256(material.encode("ascii")).hexdigest()[:8]


def ping_animation(color: str, width: int) -> bytes:
    return encode_anim(width, ANIMATION_HEIGHT, PING_FPS, ping_frames(parse_rgb(color), width))


# ---- celebration (FINISHED): flashing "!!" after the label, a rainbow rail, fireworks on the right

BANG_WIDTH = 3  # two 1px exclamation marks with a 1px gap
BANG_HEIGHT = 5  # bar, bar, bar, gap, dot: the title font's "!"
BANG_FPS = 8
BANG_COLORS: List[Rgb] = [(0, 255, 102), (255, 224, 64), (255, 255, 255), (60, 220, 255), (255, 80, 160)]


def bang_frames() -> List[bytes]:
    frames = []
    for step in range(len(BANG_COLORS)):  # the pair cycles through the palette; one lap loops
        rows = []
        for y in range(BANG_HEIGHT):
            row = bytearray(BANG_WIDTH * 3)
            if y != 3:  # the gap row stays dark
                for x, shift in ((0, 0), (2, 2)):
                    r, g, b = BANG_COLORS[(step + shift) % len(BANG_COLORS)]
                    row[x * 3:x * 3 + 3] = bytes([b, g, r])
            rows.append(bytes(row))
        frames.append(b"".join(rows))
    return frames


def bang_animation() -> bytes:
    return encode_anim(BANG_WIDTH, BANG_HEIGHT, BANG_FPS, bang_frames())


RAINBOW_FRAMES = 30
RAINBOW_FPS = 15


def rainbow_frames(width: int, height: int = ANIMATION_HEIGHT, frames: int = RAINBOW_FRAMES) -> List[bytes]:
    """A rail of flowing colors with a bright highlight sweeping along it; seamless loop."""
    out: List[bytes] = []
    for frame in range(frames):
        rows = []
        for index in range(height):
            gain = ANIMATION_ROW_GAIN[min(index, len(ANIMATION_ROW_GAIN) - 1)]
            row = bytearray()
            for x in range(width):
                hue = (x / width * 0.75 + frame / frames) % 1.0
                r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 0.9)
                shine = max(0.0, 1.0 - ((x / width - frame / frames) % 1.0) / 0.12) * 0.55
                channels = [int(min(1.0, (c + (1.0 - c) * shine)) * 255 * gain) for c in (r, g, b)]
                row += bytes([channels[2], channels[1], channels[0]])
            rows.append(bytes(row))
        out.append(b"".join(rows))
    return out


def rainbow_animation(width: int) -> bytes:
    return encode_anim(width, ANIMATION_HEIGHT, RAINBOW_FPS, rainbow_frames(width))


FIREWORKS_WIDTH = 16
FIREWORKS_HEIGHT = 12
FIREWORKS_FPS = 12
FIREWORKS_FRAMES = 30
FIREWORKS_LIFE = 14
# (center x, center y, first frame, color): three bursts a third of a cycle apart
FIREWORK_BURSTS = [(5, 6, 0, (255, 200, 40)), (11, 4, 10, (255, 80, 160)), (8, 8, 20, (60, 220, 255))]


def fireworks_frames() -> List[bytes]:
    out: List[bytes] = []
    for frame in range(FIREWORKS_FRAMES):
        canvas = [[0.0, 0.0, 0.0] for _ in range(FIREWORKS_WIDTH * FIREWORKS_HEIGHT)]

        def plot(x: float, y: float, color: Rgb, level: float) -> None:
            px, py = int(round(x)), int(round(y))
            if 0 <= px < FIREWORKS_WIDTH and 0 <= py < FIREWORKS_HEIGHT and level > 0.04:
                cell = canvas[py * FIREWORKS_WIDTH + px]
                for i in range(3):
                    cell[i] = max(cell[i], color[i] * min(1.0, level))

        for cx, cy, start, color in FIREWORK_BURSTS:
            age = ((frame - start) % FIREWORKS_FRAMES) / FIREWORKS_LIFE
            if age >= 1.0:
                continue
            spread = 1.0 - (1.0 - age) ** 2  # fast at first, then drifting
            fade = (1.0 - age) ** 0.8
            if age < 0.15:
                plot(cx, cy, (255, 255, 255), 1.0)  # the flash
            for ring, reach in ((8, 5.0), (8, 2.8)):
                for k in range(ring):
                    angle = 2 * math.pi * k / ring + (0.4 if reach < 4 else 0.0)
                    plot(cx + math.cos(angle) * reach * spread, cy + math.sin(angle) * reach * spread * 0.9, color, fade)
                    behind = max(0.0, spread - 0.12)  # a short tail
                    plot(cx + math.cos(angle) * reach * behind, cy + math.sin(angle) * reach * behind * 0.9, color, fade * 0.45)
        pixels = bytearray()
        for r, g, b in canvas:
            pixels += bytes([int(b), int(g), int(r)])
        out.append(bytes(pixels))
    return out


def fireworks_animation() -> bytes:
    return encode_anim(FIREWORKS_WIDTH, FIREWORKS_HEIGHT, FIREWORKS_FPS, fireworks_frames())


def celebration_asset_name(kind: str, *parts: object) -> str:
    """Name for a fixed (not color-configured) celebration asset; changes with ANIMATION_VERSION."""
    material = ":".join([str(ANIMATION_VERSION), kind] + [str(part) for part in parts])
    return "%s-%s.anim" % (kind, hashlib.sha256(material.encode("ascii")).hexdigest()[:8])
