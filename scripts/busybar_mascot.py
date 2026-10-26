"""Pixel-art Claude mascot animations for the left edge of the BUSY Bar front display.

Each lifecycle state gets its own looping 16x16 `.anim` (see `busybar_anim`):

    WORKING            sits behind a laptop typing, ellipsis dots pulsing overhead
    WAITING_FOR_INPUT  glances left and right, taps a foot, amber "?" bobbing
    LIMIT              worn out and sweating beside a running hourglass
    FINISHED           jumps twice with confetti, then waves with twinkling sparkles
    ERROR              dizzy X eyes, shakes, red "!" and a puff of smoke
    READY              waves hello, blinks, a text cursor blinking in the corner
    IDLE               asleep, breathing slowly, dim, little z's drifting up

The creature is 14x13 pixels and spans the display's height; the top-right corner of
the 16x16 canvas is left free for the effects. It is drawn procedurally so the art is easy to tweak; after changing a
state's animation bump its entry in `VERSIONS` so devices get a freshly named asset
(the bar refuses to overwrite a file it is displaying).
"""

from __future__ import annotations

import functools
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import busybar_anim

BASE_VERSION = 4
VERSIONS: Dict[str, int] = {}  # per-state overrides of BASE_VERSION
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

def _block(x: int, y: int, width: int, height: int) -> List[Tuple[int, int]]:
    return [(px, py) for py in range(y, y + height) for px in range(x, x + width)]


# Sprite-relative pixels (x, y) cut out of the body for each eye style. The body is 12 wide
# (x 1-12) and 9 tall (y 0-8); each eye is 2x3.
EYES: Dict[str, Sequence[Tuple[int, int]]] = {
    "open": _block(3, 2, 2, 3) + _block(9, 2, 2, 3),
    "down": _block(3, 3, 2, 3) + _block(9, 3, 2, 3),
    "left": _block(2, 2, 2, 3) + _block(8, 2, 2, 3),
    "right": _block(4, 2, 2, 3) + _block(10, 2, 2, 3),
    "blink": _block(3, 3, 2, 1) + _block(9, 3, 2, 1),
    "happy": [(3, 2), (4, 2), (2, 3), (5, 3), (9, 2), (10, 2), (8, 3), (11, 3)],
    "dead": [(3, 2), (5, 2), (4, 3), (3, 4), (5, 4), (9, 2), (11, 2), (10, 3), (9, 4), (11, 4)],
    "sleep": _block(2, 3, 4, 1) + _block(8, 3, 4, 1),
}
LEG_XS = (1, 4, 8, 11)  # four 2px legs in two pairs
FULL_LEGS = (4, 4, 4, 4)
ARM_XS = (0, 13)

GLYPH_QUESTION = ("XX.", "..X", ".X.", "...", ".X.")
GLYPH_BANG = (".X.", ".X.", ".X.", "...", ".X.")
GLYPH_Z = ("XXX", ".X.", "XXX")
GLYPH_HOURGLASS = ("XXX", "X.X", ".X.", "X.X", "XXX")

# The creature stands at (0, CREATURE_Y): 14 wide, 13 tall, so it spans the display's height
# down to the floor line, and the top-right corner (x 13-15, y 0-4) stays free for effects.
CREATURE_Y = 2
GLYPH_X = 13


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
    legs: Sequence[int] = FULL_LEGS,
    top: int = 0,
) -> None:
    """Draw the mascot with its top-left at (ox, oy).

    `arms` shifts each side nub down (negative = raised), `legs` is each leg's length in
    rows, `top` lowers the top of the body to squash it.
    """
    canvas.rect(ox + 1, oy + top, ox + 12, oy + 8, ORANGE)
    for x, dy in zip(ARM_XS, arms):
        canvas.rect(ox + x, oy + 3 + dy, ox + x, oy + 5 + dy, ORANGE)
    for x, length in zip(LEG_XS, legs):
        if length > 0:
            canvas.rect(ox + x, oy + 9, ox + x + 1, oy + 8 + length, SHADE)
    for x, y in EYES[eyes]:
        canvas.put(ox + x, oy + y + top, BLACK)


def shadow(canvas: Canvas, narrow: bool = False) -> None:
    canvas.rect(4 if narrow else 1, SIZE - 1, 11 if narrow else 14, SIZE - 1, FLOOR)


def working() -> List[Canvas]:
    thought = [(13, 4), (14, 2), (15, 0)]  # a trail of thought bubbles rising to the corner
    frames = []
    for f in range(20):
        c = Canvas()
        shadow(c)
        phase = (f // 2) % 2
        eyes = "blink" if f == 15 else ("down" if f % 10 < 7 else "open")
        clawd(c, 0, CREATURE_Y, eyes=eyes, arms=(phase, 1 - phase))
        c.rect(2, CREATURE_Y + 7, 11, CREATURE_Y + 10, LID)  # laptop lid, seen from behind
        c.rect(0, CREATURE_Y + 11, 13, CREATURE_Y + 12, BASE)
        c.rect(6, CREATURE_Y + 8, 7, CREATURE_Y + 9, GLOW if f % 10 < 5 else (150, 90, 70))
        for x, y in thought[: (f // 4) % 4]:
            c.put(x, y, DOTS)
        frames.append(c)
    return frames


def waiting() -> List[Canvas]:
    gaze = ["open"] * 6 + ["left"] * 6 + ["open"] * 3 + ["right"] * 6 + ["open"] * 5 + ["blink"] + ["open"] * 3
    frames = []
    for f in range(30):
        c = Canvas()
        shadow(c)
        tap = 4 if (f // 2) % 2 else 3
        clawd(c, 0, CREATURE_Y, eyes=gaze[f], legs=(4, 4, 4, tap))
        if f % 10 < 8:
            c.glyph(GLYPH_X, (f // 5) % 2, GLYPH_QUESTION, AMBER)
        frames.append(c)
    return frames


def limit() -> List[Canvas]:
    frames = []
    for f in range(32):
        c = Canvas()
        shadow(c)
        breathe = 1 if (f // 8) % 2 else 0
        clawd(c, 0, CREATURE_Y, eyes="blink", arms=(2 + breathe, 2 + breathe), top=breathe)
        c.glyph(GLYPH_X, 0, GLYPH_HOURGLASS, VIOLET)
        half = (f // 8) % 2  # sand runs down, then the glass flips
        c.put(GLYPH_X + 1, 3 if half else 1, SAND)
        c.put(GLYPH_X + 1, 2, SAND if f % 4 < 2 else VIOLET)  # grain in the neck
        c.put(0, 1 + (f // 2) % 4, SWEAT)  # sweat drop sliding down beside the head
        frames.append(c)
    return frames


def finished() -> List[Canvas]:
    jump = [0, 1, 2, 2, 2, 1, 0, 0] * 2
    pieces = [(1, 0, GREEN), (4, 3, YELLOW), (7, 1, WHITE), (10, 4, GREEN), (13, 2, YELLOW), (15, 6, WHITE)]
    frames = []
    for f in range(40):
        c = Canvas()
        height = jump[f] if f < 16 else 0
        shadow(c, narrow=height >= 2)
        if f < 24:
            for i, (x, phase, color) in enumerate(pieces):
                c.put(x, (f + phase * 2 + 3 * i) % 15, color)
        else:
            for i, (x, y) in enumerate([(0, 1), (15, 2), (15, 9), (1, 0)]):
                if (f + 2 * i) % 6 < 3:
                    c.put(x, y, WHITE if i % 2 else GREEN)
        if height:
            arms, legs = (-3, -3), (2, 2, 2, 2)
        elif f >= 16:
            wave = (f // 3) % 2
            arms, legs = (-3 + wave, -2 - wave), FULL_LEGS
        else:
            arms, legs = (0, 0), FULL_LEGS
        clawd(c, 0, CREATURE_Y - height, eyes="happy", arms=arms, legs=legs)
        frames.append(c)
    return frames


def error() -> List[Canvas]:
    frames = []
    for f in range(24):
        c = Canvas()
        shadow(c)
        dx = (f % 2) if f < 12 else 0  # shake right and back; the left edge has no room
        clawd(c, dx, CREATURE_Y, eyes="dead", arms=(2, 2))
        if f % 4 < 3:
            c.glyph(GLYPH_X, 0, GLYPH_BANG, RED)
        if f >= 12:
            c.put(4 + (f // 3) % 3, 1 if f % 6 < 3 else 0, SMOKE)
            c.put(9 - (f // 3) % 3, 0 if f % 6 < 3 else 1, SMOKE)
        frames.append(c)
    return frames


def ready() -> List[Canvas]:
    frames = []
    for f in range(30):
        c = Canvas()
        shadow(c)
        wave = (f // 2) % 2 if f < 10 else 0
        arms = (0, -3 + wave) if f < 10 else (0, 0)
        clawd(c, 0, CREATURE_Y, eyes="blink" if f == 20 else "open", arms=arms)
        if f % 10 < 6:
            c.rect(15, 0, 15, 3, CYAN)  # blinking text cursor: ready for input
        frames.append(c)
    return frames


def idle() -> List[Canvas]:
    frames = []
    for f in range(24):
        c = Canvas()
        shadow(c)
        squash = 1 if (f // 6) % 2 else 0
        clawd(c, 0, CREATURE_Y, eyes="sleep", arms=(squash, squash), legs=(2, 2, 2, 2), top=squash)
        for k in range(2):
            progress = ((f + 12 * k) % 24) / 24.0
            if progress < 0.85:
                c.glyph(GLYPH_X, 2 - int(progress * 3), GLYPH_Z, SLEEP)
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


# Short names: the device refuses to remove files whose full path is over ~64 characters.
SLUGS = {"WAITING_FOR_INPUT": "waiting"}


def asset_name(status: str) -> str:
    slug = SLUGS.get(status, status.lower())
    return "mascot-%s-v%d.anim" % (slug, VERSIONS.get(status, BASE_VERSION))
