"""Audio playback: one PortAudio output stream on a worker thread.

The asyncio side enqueues PCM chunks; the writer thread blocks on
stream.write(), which provides pacing.  written_seconds counts samples handed
to PortAudio and is the clock the linear subtitle reveal runs on.  clear()
drops everything still pending (control unit "stop"); in-flight written audio
cannot be un-played but stops within one chunk (~50 ms).
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Protocol

LOGGER = logging.getLogger(__name__)

_CHUNK_SAMPLES = 800  # 50 ms at 16 kHz


class PlayerSink(Protocol):
    """What PresenterApp needs from a player; tests inject a silent placeholder."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def enqueue(self, chunk: bytes) -> None: ...
    def clear(self) -> None: ...
    @property
    def written_seconds(self) -> float: ...
    def idle(self) -> bool: ...


class AudioPlayer:
    """sounddevice-backed sink; constructed lazily so headless tests stay green."""

    def __init__(self, device: str | None = None, sample_rate: int = 16_000) -> None:
        self._device = device
        self._sample_rate = sample_rate
        self._pending: queue.Queue[bytes | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._written_samples = 0
        self._lock = threading.Lock()
        self._stream: object | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="audio-out", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._pending.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def enqueue(self, chunk: bytes) -> None:
        self._pending.put(chunk)

    def clear(self) -> None:
        """Drop all pending audio (control unit stop)."""
        try:
            while True:
                self._pending.get_nowait()
        except queue.Empty:
            pass

    @property
    def written_seconds(self) -> float:
        with self._lock:
            return self._written_samples / self._sample_rate

    def idle(self) -> bool:
        """True when nothing is pending and the last chunk went to PortAudio."""
        return self._pending.empty()

    def _run(self) -> None:
        import sounddevice

        try:
            stream: Any = sounddevice.RawOutputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                device=self._device,
            )
            self._stream = stream
            with stream:
                while True:
                    chunk = self._pending.get()
                    if chunk is None:
                        return
                    offset = 0
                    while offset < len(chunk):
                        piece = chunk[offset : offset + _CHUNK_SAMPLES * 2]
                        stream.write(piece)
                        offset += len(piece)
                        with self._lock:
                            self._written_samples += len(piece) // 2
        except Exception as exc:  # device missing must not kill the host
            LOGGER.error("audio output thread stopped: %s", exc)


__all__ = ["AudioPlayer", "PlayerSink"]
