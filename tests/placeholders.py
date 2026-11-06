"""Test-only placeholders: silent player, fake TTS engine, deck fixtures.

Placeholders never ship in src (workspace rule); real synthesis is covered by
the `real`-marked smoke tests against DashScope.
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
import zlib
from collections.abc import AsyncGenerator
from pathlib import Path

from presenter.playback import PlayerSink
from presenter.presentation import PresentationBridge
from presenter.tts import TtsError

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

    async def stream(self, text: str) -> AsyncGenerator[bytes, None]:
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


class RecordingBridge(PresentationBridge):
    """Bridge double that records commands (with arrival times) instead of
    sending them to a window."""

    def __init__(self) -> None:
        super().__init__(port=0)
        self.commands: list[dict] = []
        self.arrived_at: list[float] = []

    async def send(self, command: dict) -> None:  # type: ignore[override]
        self.commands.append(command)
        self.arrived_at.append(time.monotonic())


def make_png(width: int = 2, height: int = 2) -> bytes:
    """A real, minimal RGB PNG (loads in any PNG decoder)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data))
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x7F\x80\x81" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class ScriptedEngine:
    """Engine whose per-call behaviour follows a script: ok / fail / mid.

    "fail" raises before any audio; "mid" yields one chunk, then fails.
    """

    def __init__(self, name: str, script: list[str]) -> None:
        self.name = name
        self.script = script
        self.calls = 0

    async def stream(self, text: str) -> AsyncGenerator[bytes, None]:
        self.calls += 1
        mode = self.script.pop(0) if self.script else "ok"
        if mode == "fail":
            raise TtsError(f"{self.name} offline")
        yield f"{self.name}:".encode() + b"\x00\x00" * 8
        if mode == "mid":
            raise TtsError(f"{self.name} dropped mid-stream")


def write_deck(
    root: Path,
    name: str = "demo",
    pages: int = 3,
    scripts: list[str] | None = None,
    summaries: list[str] | None = None,
) -> Path:
    """Write a valid prepared deck (manifest + real PNG pages) under root."""
    directory = root / name
    directory.mkdir(parents=True)
    page_names = [f"{index:03d}.png" for index in range(1, pages + 1)]
    for page_name in page_names:
        (directory / page_name).write_bytes(make_png())
    manifest = {
        "schema_version": (
            "1.2.0" if summaries else "1.1.0" if scripts else "1.0.0"
        ),
        "title": name,
        "total_pages": pages,
        "pages": page_names,
    }
    if scripts:
        manifest["scripts"] = scripts
    if summaries:
        manifest["summaries"] = summaries
    (directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return root
