"""AutoTtsEngine: cloud-primary with automatic local degradation (ADR-0001).

auto mode runs a primary (cloud CosyVoice) and a fallback (audio.cpp).  A
sentence whose stream fails before any audio was produced is retried
transparently on the other engine, so the listener hears no gap.  Every
primary failure counts; after `fail_after` consecutive failures the engine
degrades to the fallback.  Once `retry_after_seconds` have passed, the next
sentence probes the primary again and a clean stream restores it.  `name`
reports the currently active engine for status.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import Protocol

from presenter.tts import TtsEngine

LOGGER = logging.getLogger(__name__)


class Clock(Protocol):
    def __call__(self) -> float: ...


class AutoTtsEngine:
    def __init__(
        self,
        primary: TtsEngine,
        fallback: TtsEngine | None = None,
        *,
        fail_after: int = 3,
        retry_after_seconds: float = 60.0,
        clock: Clock = time.monotonic,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fail_after = fail_after
        self._retry_after = retry_after_seconds
        self._clock = clock
        self._active: TtsEngine = primary
        self._failures = 0
        self._degraded_at: float | None = None
        self.name: str = primary.name  # status-visible: engine actually in use

    async def stream(self, text: str) -> AsyncGenerator[bytes, None]:
        chain = self._attempt_chain()
        for index, engine in enumerate(chain):
            produced = False
            try:
                async for chunk in engine.stream(text):
                    produced = True
                    yield chunk
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_failure(engine)
                last = index == len(chain) - 1
                if produced or last:
                    raise
                LOGGER.warning(
                    "%s failed before first audio (%s); retrying on %s",
                    engine.name,
                    exc,
                    chain[index + 1].name,
                )
                continue
            self._record_success(engine)
            return

    def _attempt_chain(self) -> list[TtsEngine]:
        """Engines to try for one sentence, best candidate first.

        While primary-active every sentence may transparently fall through to
        the fallback (a transient cloud failure must not mean silence); the
        fail_after threshold only decides when the fallback becomes the
        default order so each sentence stops paying the failed cloud attempt.
        """
        if self._fallback is None:
            return [self._primary]
        if self._active is self._primary:
            return [self._primary, self._fallback]
        cooled_down = (
            self._degraded_at is not None
            and self._clock() - self._degraded_at >= self._retry_after
        )
        if cooled_down:
            return [self._primary, self._fallback]  # probe, then degrade again
        return [self._fallback]

    def _record_failure(self, engine: TtsEngine) -> None:
        if engine is not self._primary:
            return
        self._failures += 1
        if self._fallback is not None and self._failures >= self._fail_after:
            if self._active is self._primary:
                LOGGER.warning(
                    "degrading to %s after %d consecutive %s failures",
                    self._fallback.name,
                    self._failures,
                    self._primary.name,
                )
            self._active = self._fallback
            self._degraded_at = self._clock()
            self.name = self._fallback.name

    def _record_success(self, engine: TtsEngine) -> None:
        if engine is not self._primary:
            return
        if self._active is not self._primary:
            LOGGER.info("primary engine %s recovered", self._primary.name)
        self._failures = 0
        self._active = self._primary
        self._degraded_at = None
        self.name = self._primary.name


__all__ = ["AutoTtsEngine"]
