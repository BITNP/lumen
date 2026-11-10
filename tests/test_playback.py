"""AudioPlayer stop semantics: the writer thread cuts audio within one piece.

A fake sounddevice module stands in for the PortAudio binding (src imports it
lazily inside the writer thread); fake writes are non-blocking with a tiny
sleep so a long chunk stays in flight long enough to be cut mid-write.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
from collections.abc import Callable, Iterator
from types import ModuleType
from typing import Self

from presenter.playback import _CHUNK_SAMPLES, AudioPlayer

_RATE = 16_000
_PIECE_BYTES = _CHUNK_SAMPLES * 2
_WRITE_DELAY_SECONDS = 0.002


class FakeStream:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pieces: list[bytes] = []
        self.aborts = 0
        self.starts = 0
        self.latency = 0.05

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def start(self) -> None:
        self.starts += 1

    def abort(self, ignore_errors: bool = True) -> None:
        self.aborts += 1

    def write(self, piece: bytes) -> None:
        time.sleep(_WRITE_DELAY_SECONDS)
        with self.lock:
            self.pieces.append(piece)


@contextlib.contextmanager
def _fake_sounddevice() -> Iterator[FakeStream]:
    stream = FakeStream()
    module = ModuleType("sounddevice")
    module.RawOutputStream = lambda **kwargs: stream  # type: ignore[attr-defined]
    saved = sys.modules.get("sounddevice")
    sys.modules["sounddevice"] = module
    try:
        yield stream
    finally:
        if saved is None:
            del sys.modules["sounddevice"]
        else:
            sys.modules["sounddevice"] = saved


def _wait(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return predicate()


def test_stop_before_thread_start_silences_stale_and_plays_replacement() -> None:
    """stop 先落：stop 前排队的旧 chunk 一帧不出声，替换句完整播出。"""
    with _fake_sounddevice() as stream:
        player = AudioPlayer()
        player.enqueue(b"\x01" * _PIECE_BYTES * 10)  # a multi-second TTS frame
        player.clear()  # control unit stop lands before any audio goes out
        player.enqueue(b"\x02" * _PIECE_BYTES * 2)  # replacement sentence
        player.start()
        assert _wait(lambda: len(stream.pieces) == 2), "replacement audio never played"
        player.stop()
        assert all(piece == b"\x02" * _PIECE_BYTES for piece in stream.pieces)
        assert stream.aborts == 1  # device drained once, before the first piece


def test_stop_during_write_cuts_chunk_within_one_piece() -> None:
    """stop 落在写出途中：当前 chunk 最多再写一个在飞的 piece，之后新句照播。"""
    with _fake_sounddevice() as stream:
        player = AudioPlayer()
        player.start()
        player.enqueue(b"\x01" * _PIECE_BYTES * 100)  # 5 s of audio, one frame
        assert _wait(lambda: len(stream.pieces) >= 2), "writer never started"
        player.clear()
        assert _wait(lambda: stream.aborts == 1), "device FIFO never drained"
        written_at_cut = len(stream.pieces)
        player.enqueue(b"\x02" * _PIECE_BYTES * 3)  # replacement sentence
        assert _wait(lambda: len(stream.pieces) >= written_at_cut + 3)
        player.stop()
        # cut happens within the in-flight piece, not at chunk end (100 pieces)
        assert written_at_cut <= 6
        assert all(piece == b"\x02" * _PIECE_BYTES for piece in stream.pieces[-3:])
