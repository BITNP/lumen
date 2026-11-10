"""Audio playback: one PortAudio output stream on a worker thread.

The asyncio side enqueues PCM chunks; the writer thread blocks on
stream.write(), which provides pacing.  written_seconds counts samples handed
to PortAudio and is the clock the linear subtitle reveal runs on.

Stop semantics (control unit "stop"): clear() bumps a generation counter and
raises a flush flag; the writer thread owns the stream (aborting it from the
asyncio thread could deadlock a write blocked on a full buffer).  The writer
drains the device FIFO (Pa_AbortStream discards queued frames, then the
stream restarts), silently drops any queued chunk stamped before the stop,
and abandons the rest of the chunk in hand at the next piece boundary.
Audible lag after clear() is bounded by the ~50 ms piece being written, not
by chunk size (TTS delivery frames can hold seconds of audio).

Aborted-but-counted samples keep written_seconds monotonic; every consumer
takes differences against a started_at captured after the flush, so the
discarded tail never skews subtitle pacing.
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
        self._pending: queue.Queue[tuple[bytes, int] | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._written_samples = 0
        self._lock = threading.Lock()
        self._generation = 0
        self._flush = threading.Event()

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
        with self._lock:
            generation = self._generation
        self._pending.put((chunk, generation))

    def clear(self) -> None:
        """Stop current and queued audio (control unit stop).

        The writer thread performs the audible cut within one ~50 ms piece;
        chunks enqueued after this call carry a newer generation and play.
        """
        with self._lock:
            self._generation += 1
        self._flush.set()

    @property
    def written_seconds(self) -> float:
        with self._lock:
            return self._written_samples / self._sample_rate

    def idle(self) -> bool:
        """True when nothing is pending and the last chunk went to PortAudio."""
        return self._pending.empty()

    def _drain_device(self, stream: Any) -> None:
        """Discard device-buffered audio, then resume the stream.

        sounddevice abort() ignores errors by default and start() tolerates an
        already-running stream, so this is safe at every piece boundary.
        """
        stream.abort()
        self._flush.clear()
        stream.start()

    def _run(self) -> None:
        import sounddevice

        try:
            stream: Any = sounddevice.RawOutputStream(
                samplerate=self._sample_rate,
                channels=1,
                dtype="int16",
                device=self._device,
            )
            with stream:
                while True:
                    item = self._pending.get()
                    if item is None:
                        return
                    chunk, generation = item
                    if self._flush.is_set():
                        # A stop landed while idle or with stale chunks queued:
                        # cut the device buffer before anything new is written.
                        self._drain_device(stream)
                    with self._lock:
                        current = self._generation
                    if generation < current:
                        continue  # queued before the stop: never let it sound
                    offset = 0
                    while offset < len(chunk):
                        if self._flush.is_set():
                            # The flag was raised after this chunk passed its
                            # generation check, so the rest of it is stale.
                            self._drain_device(stream)
                            break
                        piece = chunk[offset : offset + _CHUNK_SAMPLES * 2]
                        stream.write(piece)
                        offset += len(piece)
                        with self._lock:
                            self._written_samples += len(piece) // 2
        except Exception as exc:  # noqa: BLE001 - 声卡异常不得杀死宿主：任何输出故障都收敛为日志并停线程
            LOGGER.error("audio output thread stopped: %s", exc)


__all__ = ["AudioPlayer", "PlayerSink"]
