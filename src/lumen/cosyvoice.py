"""DashScope CosyVoice engine (cloud, primary per ADR-0001).

Official DashScope SDK streaming synthesis; the SDK's worker-thread callbacks
are bridged into an async generator via asyncio queues.  The call shape
(SpeechSynthesizer + streaming_call/streaming_complete, PCM_16000HZ_MONO_16BIT,
cancel via streaming_cancel + close in finally) follows the previously
production-verified Raspberry Girl core adapter.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
import threading
import warnings
from collections.abc import AsyncGenerator
from typing import Any

# DashScope imports the deprecated Assistants API at package level; only TTS
# v2 is used here, so exempt exactly that third-party import warning.
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"The Assistants API \(dashscope.assistants\) is deprecated.*",
        category=DeprecationWarning,
        module=r"dashscope",
    )
    import dashscope
    from dashscope.audio.tts_v2 import AudioFormat, ResultCallback, SpeechSynthesizer

from lumen.tts import TtsError

_MAX_AUDIO_BYTES = 16_000 * 2 * 180  # hard cap: 180 s of 16 kHz mono s16le


class _ChunkBridge(ResultCallback):
    """SDK callback thread -> bounded thread queue; overflow and errors fail fast."""

    def __init__(self, capacity: int = 256) -> None:
        self.chunks: queue.Queue[bytes] = queue.Queue(capacity)
        self.done = threading.Event()
        self.failed = threading.Event()

    def on_data(self, data: bytes) -> None:
        if self.done.is_set():
            return
        try:
            self.chunks.put_nowait(data)
        except queue.Full:
            self.failed.set()
            self.done.set()

    def on_complete(self) -> None:
        self.done.set()

    def on_error(self, message: str) -> None:
        # Provider error text may echo request content; never log or forward it.
        self.failed.set()
        self.done.set()

    def on_close(self) -> None:
        if not self.done.is_set():
            self.failed.set()
            self.done.set()


class CosyVoiceEngine:
    """Streams one sentence per stream() call; at most two requests in flight."""

    name = "cosyvoice"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        voice: str,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._model = model
        self._voice = voice
        self._timeout = timeout_seconds
        self._slots = asyncio.Semaphore(2)
        dashscope.api_key = api_key

    async def stream(self, text: str) -> AsyncGenerator[bytes, None]:
        async with self._slots, asyncio.timeout(self._timeout):
            bridge = _ChunkBridge()
            synth: Any = SpeechSynthesizer(
                model=self._model,
                voice=self._voice,
                format=AudioFormat.PCM_16000HZ_MONO_16BIT,
                callback=bridge,
            )

            def run() -> None:
                try:
                    synth.streaming_call(text)
                    synth.streaming_complete(int(self._timeout * 1000))
                except Exception:  # noqa: BLE001 - dashscope SDK 线程：任何失败都置位交给消费侧报 TtsError
                    bridge.failed.set()
                    bridge.done.set()

            worker = asyncio.create_task(asyncio.to_thread(run))
            total = 0
            try:
                while not bridge.done.is_set() or not bridge.chunks.empty():
                    if bridge.failed.is_set():
                        raise TtsError("cosyvoice synthesis failed or buffer overflow")
                    try:
                        chunk = bridge.chunks.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.01)
                        continue
                    total += len(chunk)
                    if total > _MAX_AUDIO_BYTES or len(chunk) % 2:
                        raise TtsError("cosyvoice audio exceeded limits or bad PCM")
                    yield chunk
                if bridge.failed.is_set() or total == 0:
                    raise TtsError("cosyvoice produced no audio")
            finally:
                bridge.done.set()

                def close() -> None:
                    with contextlib.suppress(Exception):  # 取消尽力而为
                        synth.streaming_cancel(1000)
                    synth.close()

                await asyncio.to_thread(close)
                try:
                    await asyncio.wait_for(asyncio.shield(worker), timeout=2)
                except TimeoutError:
                    worker.cancel()


__all__ = ["CosyVoiceEngine"]
