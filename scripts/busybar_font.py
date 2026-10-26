"""Text metrics for the BUSY Bar front display.

The device has no API to measure text, and the dots after "WAITING" must sit exactly one
pixel after the last letter of a label centered as a unit, so the glyph widths of the
`small` (title) font are recorded here. They were measured on a real bar by drawing each
printable ASCII character alone and reading the framebuffer back; strings measure as the
sum of glyph widths plus one pixel between glyphs, with a space being two pixels.
"""

from __future__ import annotations

# Pixel width of each glyph in `small` for the characters "!" (33) through "~" (126).
SMALL_WIDTHS = "1353441223312123233333333123333444444444134354444443435333222336333332331231533332323353333134"
SMALL_SPACE = 2
UNKNOWN_GLYPH = 5
TINY_ADVANCE = 4  # `tiny` is not tabulated; this is its measured average advance


def text_width(text: str, font: str = "small") -> int:
    """Width in pixels of `text` as the device draws it (exact for `small`, an estimate otherwise)."""
    if font != "small":
        return len(text) * TINY_ADVANCE
    total = 0
    for char in text:
        if char == " ":
            total += SMALL_SPACE
        elif 33 <= ord(char) <= 126:
            total += int(SMALL_WIDTHS[ord(char) - 33]) + 1
        else:
            total += UNKNOWN_GLYPH + 1
    if text and text[-1] != " ":
        total -= 1  # no gap after the last glyph
    return total
