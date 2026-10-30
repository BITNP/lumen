"""Test-only placeholders: silent player + fake TTS engine.

Placeholders never ship in src (workspace rule); real synthesis is covered by
the `real`-marked smoke tests against DashScope.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from presenter.playback import PlayerSink

SILENT_RATE_BYTES = 32_000  # 16 kHz mono s16le


class SilentPlayer(PlayerSink):
    """Instant-consumption player: written clock advances with enqueue."""

    def __init__(self) -> None:
        self.enqueued: list[bytes] = []
        self.cleared = 0
        self._written = 0.0
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def enqueue(self, chunk: bytes) -> None:
        self.enqueued.append(chunk)
        self._written += len(chunk) / SILENT_RATE_BYTES

    def clear(self) -> None:
        self.cleared += 1

    @property
    def written_seconds(self) -> float:
        return self._written

    def idle(self) -> bool:
        return True


class FakeEngine:
    """Deterministic silence; optionally slow, to exercise stop/cancel."""

    name = "fake"

    def __init__(self, *, chunk_seconds: float = 0.1, chunks: int = 3, delay: float = 0.0) -> None:
        self.calls: list[str] = []
        self._size = int(SILENT_RATE_BYTES * chunk_seconds)
        self._chunks = chunks
        self._delay = delay

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        self.calls.append(text)
        for _ in range(self._chunks):
            if self._delay:
                await asyncio.sleep(self._delay)
            yield b"\x00\x00" * self._size


class RecordingClient:
    """Stands in for PresenterClient inside PresenterApp tests."""

    def __init__(self) -> None:
        self.inputs: list[dict] = []
        self.results: list[tuple[str, str]] = []

    async def send_input(
        self, content: str, *, source: str = "presenter", metadata: dict | None = None
    ) -> None:
        self.inputs.append({"content": content, "source": source, "metadata": metadata or {}})

    async def send_result(self, status: str, detail: str) -> None:
        self.results.append((status, detail))
