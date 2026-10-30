"""Linear reveal subtitle math (pure functions, no device or clock access).

Subtitles are revealed uniformly against the real playback clock (ADR-0010:
no word-level timestamps).  The reveal advances one unit at a time: a CJK or
other non-latin run reveals per grapheme, latin/digits reveal per word, so a
revealed prefix always ends on a unit boundary.  This mirrors the reveal-unit
rule the previous Raspberry Girl core used for its subtitle channel.
"""
from __future__ import annotations

# Fallback speaking rate (reveal units per second) used before the true audio
# duration of a sentence is known; roughly Mandarin narration speed.
FALLBACK_UNITS_PER_SECOND = 3.5


def reveal_units(text: str) -> list[tuple[int, int]]:
    """Split text into reveal units as (start, end) character slices."""
    units: list[tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            # Whitespace rides along with the following unit's prefix slice.
            units.append((index, index + 1))
            index += 1
            continue
        if char.isascii() and (char.isalnum()):
            end = index
            while end < length and text[end].isascii() and text[end].isalnum():
                end += 1
            units.append((index, end))
            index = end
            continue
        units.append((index, index + 1))
        index += 1
    return units


def reveal_visible_chars(text: str, elapsed_ms: int, duration_ms: int) -> int:
    """Number of leading characters visible at elapsed_ms into a duration_ms line.

    Uniform pacing across reveal units; the whole line is visible once the
    estimated/planned duration has elapsed.
    """
    units = reveal_units(text)
    if not units:
        return len(text)
    if duration_ms <= 0:
        rate_units = FALLBACK_UNITS_PER_SECOND * (elapsed_ms / 1000.0)
        shown = int(rate_units)
    else:
        shown = int(len(units) * elapsed_ms / duration_ms)
    shown = max(0, min(shown, len(units)))
    return units[shown - 1][1] if shown else 0


def estimate_duration_ms(text: str, audio_bytes: int | None = None) -> int:
    """Sentence duration: exact from PCM length when known, else the fallback rate."""
    units = max(1, len(reveal_units(text)))
    if audio_bytes is not None and audio_bytes > 0:
        # PCM 16 kHz mono s16le: 32000 bytes per second.
        return max(1, int(audio_bytes / 32000 * 1000))
    return max(1, int(units / FALLBACK_UNITS_PER_SECOND * 1000))


__all__ = ["FALLBACK_UNITS_PER_SECOND", "reveal_units", "reveal_visible_chars", "estimate_duration_ms"]
