"""Synthesized notification sounds for the BUSY Bar speaker.

The bar plays `.snd` files: raw 16-bit little-endian mono PCM at 44.1 kHz with no header
(checked against the sounds that ship on the device). The sounds are generated here from a
few bell-like tones, so no audio files ship with the plugin and nothing needs ffmpeg.

The device volume is a shared, global setting, so the plugin never changes it. Instead the
sounds are quiet by construction: `GAIN` is the peak level as a fraction of full scale.
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import List, Sequence, Tuple

SAMPLE_RATE = 44100
GAIN = 0.25  # peak level (fraction of full scale); tune by ear at the usual device volume
ATTACK = 0.006  # seconds: fade-in, so a note does not click
RELEASE = 0.02  # seconds: fade-out at the end of every note
SOUND_VERSION = 1

# (frequency in Hz, start offset in seconds, duration in seconds, relative level)
Note = Tuple[float, float, float, float]
C5, E5, G5, A5, C6 = 523.25, 659.25, 783.99, 880.0, 1046.50

WAITING_NOTES: Sequence[Note] = [(A5, 0.0, 0.22, 1.0), (E5, 0.17, 0.42, 0.9)]  # "ding-dong": a falling fourth
FINISHED_NOTES: Sequence[Note] = [
    (C5, 0.00, 0.30, 0.8),
    (E5, 0.08, 0.30, 0.8),
    (G5, 0.16, 0.30, 0.85),
    (C6, 0.24, 0.55, 1.0),  # the last note rings out
]
KINDS = {"waiting": WAITING_NOTES, "finished": FINISHED_NOTES}


def bell(frequency: float, seconds: float) -> List[float]:
    """One bell-like note: a sine with decaying overtones and a click-free envelope."""
    count = int(SAMPLE_RATE * seconds)
    samples = []
    for i in range(count):
        t = i / SAMPLE_RATE
        value = (
            math.sin(2 * math.pi * frequency * t)
            + 0.35 * math.sin(2 * math.pi * 2 * frequency * t) * math.exp(-6 * t)
            + 0.12 * math.sin(2 * math.pi * 3 * frequency * t) * math.exp(-10 * t)
        )
        envelope = math.exp(-4.0 * t / seconds)
        envelope *= min(1.0, t / ATTACK)
        envelope *= min(1.0, (seconds - t) / RELEASE)
        samples.append(value * envelope)
    return samples


def render(notes: Sequence[Note]) -> bytes:
    """Mix the notes and return 16-bit little-endian mono PCM, peaking at GAIN of full scale."""
    total = max(int(SAMPLE_RATE * (start + length)) for _, start, length, _ in notes) + 1
    mix = [0.0] * total
    for frequency, start, length, level in notes:
        offset = int(SAMPLE_RATE * start)
        for i, value in enumerate(bell(frequency, length)):
            mix[offset + i] += value * level
    peak = max(abs(value) for value in mix) or 1.0
    scale = GAIN * 32767 / peak
    return struct.pack("<%dh" % total, *(int(round(value * scale)) for value in mix))


def sound(kind: str) -> bytes:
    return render(KINDS[kind])


def sound_asset_name(kind: str) -> str:
    material = "%d:%d:%s:%r" % (SOUND_VERSION, SAMPLE_RATE, GAIN, KINDS[kind])
    return "sound-%s-%s.snd" % (kind, hashlib.sha256(material.encode("ascii")).hexdigest()[:8])
