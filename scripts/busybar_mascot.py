"""Pixel-art Claude mascot animations for the left edge of the BUSY Bar front display.

Each lifecycle state gets its own looping 16x16 `.anim` (see `busybar_anim`):

    WORKING            sits behind a laptop typing, ellipsis dots pulsing overhead
    WAITING_FOR_INPUT  glances left and right, taps a foot, amber "?" bobbing
    LIMIT              worn out and sweating beside a running hourglass
    FINISHED           jumps twice with confetti, then waves with twinkling sparkles
    ERROR              dizzy X eyes, shakes, red "!" and a puff of smoke
    READY              waves hello, blinks, a ">_" prompt blinking overhead
    IDLE               asleep, breathing slowly, dim, little z's drifting up

The sprite is drawn procedurally so the art is easy to tweak; after changing a
state's animation bump its entry in `VERSIONS` so devices get a freshly named asset
(the bar refuses to overwrite a file it is displaying).
"""

from __future__ import annotations

import functools
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import busybar_anim

BASE_VERSION = 2
VERSIONS: Dict[str, int] = {"WORKING": 3}  # per-state overrides of BASE_VERSION
SIZE = 16

Rgb = Tuple[int, int, int]

ORANGE: Rgb = (235, 112, 64)
SHADE: Rgb = (170, 76, 40)
BLACK: Rgb = (0, 0, 0)
FLOOR: Rgb = (36, 38, 46)
LID: Rgb = (92, 98, 114)
BASE: Rgb = (56, 60, 70)
GLOW: Rgb = (255, 196, 160)
WHITE: Rgb = (255, 255, 255)
AMBER: Rgb = (255, 176, 0)
RED: Rgb = (255, 40, 40)
GREEN: Rgb = (0, 255, 102)
YELLOW: Rgb = (255, 224, 64)
DOTS: Rgb = (120, 190, 255)
SMOKE: Rgb = (120, 120, 126)
SLEEP: Rgb = (150, 150, 175)
CYAN: Rgb = (0, 200, 208)
VIOLET: Rgb = (170, 110, 255)
SAND: Rgb = (255, 190, 70)
SWEAT: Rgb = (110, 200, 255)

# Sprite-relative pixels (x, y) cut out of the body for each eye style.
EYES: Dict[str, Sequence[Tuple[int, int]]] = {
    "open": [(3, 1), (3, 2), (8, 1), (8, 2)],
    "down": [(3, 2), (3, 3), (8, 2), (8, 3)],
    "left": [(2, 1), (2, 2), (7, 1), (7, 2)],
    "right": [(4, 1), (4, 2), (9, 1), (9, 2)],
    "blink": [(3, 2), (8, 2)],
    "happy": [(2, 2), (3, 1), (4, 2), (7, 2), (8, 1), (9, 2)],
    "dead": [(2, 1), (4, 1), (3, 2), (2, 3), (4, 3), (7, 1), (9, 1), (8, 2), (7, 3), (9, 3)],
    "sleep": [(2, 2), (3, 2), (4, 2), (7, 2), (8, 2), (9, 2)],
}
LEG_XS = (2, 4, 7, 9)

GLYPH_QUESTION = ("XX.", "..X", ".X.", "...", ".X.")
GLYPH_BANG = (".X.", ".X.", ".X.", "...", ".X.")
GLYPH_Z = ("XXX", ".X.", "XXX")
GLYPH_PROMPT = ("X..", ".X.", "..X", ".X.", "X..")
GLYPH_HOURGLASS = ("XXXXX", ".X.X.", "..X..", ".X.X.", "XXXXX")


class Canvas:
    def __init__(self) -> None:
        self.pixels: List[Optional[Rgb]] = [None] * (SIZE * SIZE)

    def put(self, x: int, y: int, color: Rgb) -> None:
        if 0 <= x < SIZE and 0 <= y < SIZE:
            self.pixels[y * SIZE + x] = color

    def rect(self, x0: int, y0: int, x1: int, y1: int, color: Rgb) -> None:
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                self.put(x, y, color)

    def glyph(self, x: int, y: int, rows: Sequence[str], color: Rgb) -> None:
        for dy, row in enumerate(rows):
            for dx, char in enumerate(row):
                if char == "X":
                    self.put(x + dx, y + dy, color)

    def render(self, brightness: float = 1.0) -> bytes:
        out = bytearray()
        for pixel in self.pixels:
            r, g, b = pixel or BLACK
            out += bytes([int(b * brightness), int(g * brightness), int(r * brightness)])
        return bytes(out)


def clawd(
    canvas: Canvas,
    ox: int,
    oy: int,
    eyes: str = "open",
    arms: Tuple[int, int] = (0, 0),
    legs: Sequence[int] = (2, 2, 2, 2),
    top: int = 0,
) -> None:
    """Draw the 12-pixel-wide mascot with its top-left at (ox, oy).

    `arms` shifts each side nub down (negative = raised), `legs` is each leg's
    length, `top` lowers the top of the body to squash it.
    """
    canvas.rect(ox + 1, oy + top, ox + 10, oy + 4, ORANGE)
    for x, dy in ((0, arms[0]), (11, arms[1])):
        canvas.rect(ox + x, oy + 2 + dy, ox + x, oy + 3 + dy, ORANGE)
    for x, length in zip(LEG_XS, legs):
        canvas.rect(ox + x, oy + 5, ox + x, oy + 4 + length, SHADE)
    for x, y in EYES[eyes]:
        canvas.put(ox + x, oy + y + top, BLACK)


def shadow(canvas: Canvas, narrow: bool = False) -> None:
    canvas.rect(4 if narrow else 2, SIZE - 1, 11 if narrow else 13, SIZE - 1, FLOOR)


def working() -> List[Canvas]:
    frames = []
    for f in range(20):
        c = Canvas()
        shadow(c)
        phase = (f // 2) % 2
        eyes = "blink" if f == 15 else ("down" if f % 10 < 7 else "open")
        clawd(c, 2, 8, eyes=eyes, arms=(phase, 1 - phase))
        c.rect(4, 12, 11, 13, LID)  # laptop lid, seen from behind
        c.rect(3, 14, 12, 14, BASE)
        c.rect(7, 12, 8, 12, GLOW if f % 10 < 5 else (150, 90, 70))
        for i in range((f // 4) % 4):
            c.put(5 + 3 * i, 5, DOTS)
        frames.append(c)
    return frames


def waiting() -> List[Canvas]:
    gaze = ["open"] * 6 + ["left"] * 6 + ["open"] * 3 + ["right"] * 6 + ["open"] * 5 + ["blink"] + ["open"] * 3
    frames = []
    for f in range(30):
        c = Canvas()
        shadow(c)
        tap = 1 if (f // 2) % 2 else 2
        clawd(c, 2, 8, eyes=gaze[f], legs=(2, 2, 2, tap))
        if f % 10 < 8:
            c.glyph(10, 1 + (f // 5) % 2, GLYPH_QUESTION, AMBER)
        frames.append(c)
    return frames


def finished() -> List[Canvas]:
    jump = [0, 1, 2, 3, 3, 2, 1, 0] * 2
    pieces = [(1, 0, GREEN), (4, 3, YELLOW), (7, 1, WHITE), (10, 4, GREEN), (13, 2, YELLOW), (14, 6, WHITE)]
    frames = []
    for f in range(40):
        c = Canvas()
        height = jump[f] if f < 16 else 0
        shadow(c, narrow=height >= 2)
        if f < 24:
            for i, (x, phase, color) in enumerate(pieces):
                c.put(x, (f + phase * 2 + 3 * i) % 14, color)
        else:
            for i, (x, y) in enumerate([(1, 4), (14, 3), (13, 9), (2, 10)]):
                if (f + 2 * i) % 6 < 3:
                    c.put(x, y, WHITE if i % 2 else GREEN)
        if height:
            arms, legs = (-2, -2), (1, 1, 1, 1)
        elif f >= 16:
            wave = (f // 3) % 2
            arms, legs = (-2 + wave, -1 - wave), (2, 2, 2, 2)
        else:
            arms, legs = (0, 0), (2, 2, 2, 2)
        clawd(c, 2, 8 - height, eyes="happy", arms=arms, legs=legs)
        frames.append(c)
    return frames


def error() -> List[Canvas]:
    frames = []
    for f in range(24):
        c = Canvas()
        shadow(c)
        dx = (-1 if f % 2 == 0 else 1) if f < 12 else 0
        clawd(c, 2 + dx, 8, eyes="dead", arms=(1, 1))
        if f % 4 < 3:
            c.glyph(7, 1, GLYPH_BANG, RED)
        if f >= 12:
            c.put(4 + f % 3, 7 - (f - 12) % 6, SMOKE)
            c.put(9 - f % 3, 6 - (f - 12) % 5, SMOKE)
        frames.append(c)
    return frames


def idle() -> List[Canvas]:
    frames = []
    for f in range(24):
        c = Canvas()
        shadow(c)
        squash = 1 if (f // 6) % 2 else 0
        clawd(c, 2, 8, eyes="sleep", arms=(squash, squash), legs=(1, 1, 1, 1), top=squash)
        for k in range(2):
            progress = ((f + 12 * k) % 24) / 24.0
            if progress < 0.85:
                c.glyph(10 + int(progress * 2), 5 - int(progress * 5), GLYPH_Z, SLEEP)
        frames.append(c)
    return frames


def limit() -> List[Canvas]:
    frames = []
    for f in range(32):
        c = Canvas()
        shadow(c)
        breathe = 1 if (f // 8) % 2 else 0
        clawd(c, 2, 8, eyes="blink", arms=(1 + breathe, 1 + breathe), legs=(2, 2, 2, 2), top=breathe)
        c.glyph(10, 1, GLYPH_HOURGLASS, VIOLET)
        half = (f // 8) % 2  # sand runs down, then the glass flips
        c.put(12, 2 if not half else 4, SAND)
        if f % 4 < 2:
            c.put(12, 3, SAND)  # grain in the neck
        c.put(1, 7 + (f // 2) % 4, SWEAT)  # sweat drop sliding down the side
        frames.append(c)
    return frames


def ready() -> List[Canvas]:
    frames = []
    for f in range(30):
        c = Canvas()
        shadow(c)
        wave = (f // 2) % 2 if f < 10 else 0
        arms = (0, -2 + wave) if f < 10 else (0, 0)
        clawd(c, 2, 8, eyes="blink" if f == 20 else "open", arms=arms)
        c.glyph(3, 1, GLYPH_PROMPT, CYAN)
        if f % 10 < 6:
            c.rect(7, 5, 9, 5, CYAN)  # blinking cursor
        frames.append(c)
    return frames


# state -> (frame builder, playback fps, brightness)
ANIMATIONS: Dict[str, Tuple[Callable[[], List[Canvas]], int, float]] = {
    "WORKING": (working, 10, 1.0),
    "WAITING_FOR_INPUT": (waiting, 10, 1.0),
    "LIMIT": (limit, 8, 1.0),
    "FINISHED": (finished, 10, 1.0),
    "ERROR": (error, 12, 1.0),
    "READY": (ready, 10, 1.0),
    "IDLE": (idle, 6, 0.45),
}


@functools.lru_cache(maxsize=None)
def animation(status: str) -> bytes:
    builder, fps, brightness = ANIMATIONS[status]
    return busybar_anim.encode_anim(SIZE, SIZE, fps, [frame.render(brightness) for frame in builder()])


def asset_name(status: str) -> str:
    return "mascot-%s-v%d.anim" % (status.lower().replace("_", "-"), VERSIONS.get(status, BASE_VERSION))
