"""Text metrics for the BUSY Bar front display.

The device has no API to measure text, yet several things must line up to the pixel: the
dots after "WAITING", the mode badge's pill, and where a label may be centered without
reaching the mascot. So the glyph widths of the `small` (title) and `tiny` (subtitle) fonts
are recorded here. They were measured on a real bar by drawing each printable ASCII
character alone and reading the framebuffer back; strings measure as the sum of glyph
widths plus one pixel between glyphs, with a space being 2 (small) or 3 (tiny) pixels.
"""

from __future__ import annotations

# Per font: the pixel width of each glyph for the characters "!" (33) through "~" (126), and the
# width of a space.
SMALL_WIDTHS = "1353441223312123233333333123333444444444134354444443435333222336333332331231533332323353333134"
TINY_WIDTHS = "1353441223313143333333333113333333333333334354434333455354242332333332231131533332333353333134"
METRICS = {"small": (SMALL_WIDTHS, 2), "tiny": (TINY_WIDTHS, 3)}
UNKNOWN_GLYPH = 5


def text_width(text: str, font: str = "small") -> int:
    """Width in pixels of `text` as the device draws it in `small` or `tiny`."""
    widths, space = METRICS[font]
    total = 0
    for char in text:
        if char == " ":
            total += space
        elif 33 <= ord(char) <= 126:
            total += int(widths[ord(char) - 33]) + 1
        else:
            total += UNKNOWN_GLYPH + 1
    if text and text[-1] != " ":
        total -= 1  # no gap after the last glyph
    return total
