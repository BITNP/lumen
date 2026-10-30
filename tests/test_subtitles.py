"""Linear reveal subtitle math."""
from __future__ import annotations

from presenter.subtitles import (
    estimate_duration_ms,
    reveal_units,
    reveal_visible_chars,
)


def test_reveal_units_mixed_text() -> None:
    text = "你好 HTTP 200"
    units = reveal_units(text)
    ends = [end for _, end in units]
    # CJK per char, whitespace rides alone, latin/digit words as one unit.
    assert ends == [1, 2, 3, 7, 8, 11]


def test_reveal_progresses_and_saturates() -> None:
    text = "树莓娘会按句说话"
    length = len(text)
    duration = 2000
    steps = [reveal_visible_chars(text, ms, duration) for ms in range(0, duration + 500, 250)]
    assert steps == sorted(steps)
    assert steps[0] == 0
    assert steps[-1] == length


def test_reveal_without_duration_uses_fallback_rate() -> None:
    text = "八个人"  # 3 graphemes
    assert reveal_visible_chars(text, 0, 0) == 0
    assert reveal_visible_chars(text, 10_000, 0) == len(text)


def test_estimate_duration() -> None:
    # exact from PCM length: 1 second of audio
    assert estimate_duration_ms("任何文本", 32_000) == 1000
    # unknown audio: positive fallback estimate
    assert estimate_duration_ms("任何文本") > 0
