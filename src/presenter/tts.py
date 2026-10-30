"""TtsEngine: the seam every synthesis backend implements (ADR-0001).

Engines deliver PCM 16 kHz mono signed 16-bit little-endian as async chunks;
closing the async iterator must cancel the in-flight synthesis.  Milestone M1
ships CosyVoiceEngine (cloud); the audio.cpp fallback (M4) is just a second
implementation of this protocol.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Final, Protocol

SAMPLE_RATE_HZ: Final = 16_000
BYTES_PER_SECOND: Final = SAMPLE_RATE_HZ * 2  # mono s16le


class TtsError(RuntimeError):
    """Raised when synthesis fails or produces invalid audio."""


class TtsEngine(Protocol):
    """One TTS backend; implementations may hold their own concurrency limits."""

    name: str

    def stream(self, text: str) -> AsyncIterator[bytes]:
        """Stream PCM chunks for one sentence until it is fully synthesized."""
        ...


__all__ = ["TtsEngine", "TtsError", "SAMPLE_RATE_HZ", "BYTES_PER_SECOND"]
