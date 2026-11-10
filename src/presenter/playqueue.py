"""PlaybackQueue: the unified synthesis+playback queue, centered on playing (ADR-0008).

One queue of playback tasks in arrival order; ``tasks[0]`` is the head:

- a single background synth worker walks the queue in order and synthesizes
  serially: the head streams while it plays, every later task is pre-synthesized
  and its PCM cached, so the first byte of a sentence is only ever one synth
  job away from the queue position it occupies;
- every visible event of a task (ppt, emotion/motion, subtitle, mouth) fires at
  START OF PLAY (开播): the moment its first audio byte goes to the player
  (visual-only tasks apply when they are taken as head) — never during the
  multi-second synthesis wait;
- stop (control unit) cancels the in-flight background synthesis, drops every
  task that has NOT started playing, and arms SEAMLESS TAKEOVER (无缝接管): the
  playing head is never cut by stop — it finishes, or the first post-stop task
  takes over the instant its audio is ready, so no silent window ever opens;
- the queue reports a drain once it truly empties after having played
  something (queue_finished, ADR-0005); subtitles are only ever covered,
  never cleared.

All queue mutation happens on the event loop; the writer thread behind
``PlayerSink`` stays the sole owner of the audio device. Every wait re-checks
its predicates on a tick, so a lost wakeup costs at most one tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

from presenter.playback import PlayerSink
from presenter.subtitles import estimate_duration_ms, reveal_visible_chars
from presenter.tts import BYTES_PER_SECOND, TtsEngine
from presenter.units import ExpressionUnit, PptCommand

LOGGER = logging.getLogger(__name__)

TICK_SECONDS: Final = 0.05  # subtitle ticker / predicate re-check step

Emit = Callable[[dict[str, Any]], Awaitable[None]]
ApplyPpt = Callable[[PptCommand], Awaitable[None]]
OnDrain = Callable[[], Awaitable[None]]
OnError = Callable[[str], Awaitable[None]]


class PlaybackTask:
    """One queued unit's playback state: unit, buffered PCM, synth progress."""

    def __init__(self, unit: ExpressionUnit) -> None:
        self.unit = unit
        self.audio = bytearray()
        self.synth_started = False
        self.synth_done = False
        self.error: Exception | None = None
        self.dropped = False

    @property
    def audible(self) -> bool:
        return self.unit.speech is not None

    @property
    def play_ready(self) -> bool:
        """First PCM byte in hand, or synthesis completed (possibly silent)."""
        return bool(self.audio) or self.synth_done


class _Synth:
    """One engine.stream() call pumped into its task's audio buffer.

    The pump swallows its own cancellation: a cancelled synthesis ends quietly
    (provider stream closed deterministically, ADR-0001 teardown rule) and the
    waiter resumes normally instead of seeing CancelledError leak across tasks.
    """

    def __init__(self, engine: TtsEngine, text: str, task: PlaybackTask) -> None:
        LOGGER.debug("tts request (engine=%s): %r", engine.name, text)
        self.task = task
        self._text = text
        self._engine = engine
        self._started_at = time.monotonic()
        self._first_chunk_at: float | None = None
        self._pump = asyncio.create_task(self._run())

    async def _run(self) -> None:
        stream = self._engine.stream(self._text)
        try:
            async for chunk in stream:
                if self._first_chunk_at is None:
                    self._first_chunk_at = time.monotonic()
                self.task.audio += chunk
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await stream.aclose()
            return
        except Exception as exc:  # noqa: BLE001 - 单条合成失败只报 error 结果，不杀播放任务
            LOGGER.warning(
                "tts synthesis failed after %.2fs (%s): %s",
                time.monotonic() - self._started_at,
                self._engine.name,
                exc,
            )
            self.task.error = exc
        else:
            self.task.synth_done = True
            LOGGER.debug(
                "tts synthesized %d bytes of %s audio in %.2fs (first chunk %.2fs): %r",
                len(self.task.audio),
                self._engine.name,
                time.monotonic() - self._started_at,
                (self._first_chunk_at or self._started_at) - self._started_at,
                self._text,
            )

    def cancel(self) -> None:
        self._pump.cancel()

    async def wait(self) -> None:
        await self._pump


class PlaybackQueue:
    """Unified queue: plays the head, pre-synthesizes the rest, honors stop."""

    def __init__(
        self,
        *,
        engine: TtsEngine,
        player: PlayerSink,
        emit: Emit,
        apply_ppt: ApplyPpt,
        on_drain: OnDrain,
        on_error: OnError,
        sentence_gap_seconds: float,
    ) -> None:
        self._engine = engine
        self._player = player
        self._emit = emit
        self._apply_ppt = apply_ppt
        self._on_drain = on_drain
        self._on_error = on_error
        self._sentence_gap = sentence_gap_seconds
        self._tasks: list[PlaybackTask] = []
        self._synth: _Synth | None = None
        self._synth_worker: asyncio.Task[None] | None = None
        self._playback_worker: asyncio.Task[None] | None = None
        # The task whose first byte reached the player and which has not
        # retired yet; stop keeps exactly this one alive.
        self._playing_task: PlaybackTask | None = None
        self._mouth_active = False
        # Armed by stop: the first post-stop task to go audio-ready cuts the
        # playing head (seamless takeover). Cleared when the pre-stop head
        # retires or when a new round starts from an empty queue.
        self._preempt_armed = False
        self._last_speech_end_at: float | None = None
        self._completed_since_report = 0
        self._started_since_report = 0

    # -- submission and stop -------------------------------------------------

    def submit(self, unit: ExpressionUnit) -> None:
        """Enqueue one unit's playback task at the tail (call after can_accept)."""
        if not self._tasks:
            self._preempt_armed = False  # a round from empty is a fresh start
        self._tasks.append(PlaybackTask(unit))
        self._ensure_workers()

    def can_accept(self, limit: int) -> bool:
        return len(self._tasks) < limit

    def interrupt(self) -> int:
        """Apply a control-unit stop; returns how many tasks were dropped.

        Drops every task that has not started playing (an un-spoken head
        included), cancels its in-flight synthesis, and arms takeover so the
        playing head is cut only once post-stop audio is ready (ADR-0008).
        """
        self._preempt_armed = True
        keep = 1 if self._tasks and self._tasks[0] is self._playing_task else 0
        dropped = self._tasks[keep:]
        del self._tasks[keep:]
        for task in dropped:
            task.dropped = True
        if self._synth is not None and self._synth.task.dropped:
            self._synth.cancel()
        return len(dropped)

    # -- status ----------------------------------------------------------------

    @property
    def playing(self) -> bool:
        return bool(self._tasks)

    @property
    def pending_depth(self) -> int:
        return max(0, len(self._tasks) - (1 if self._playing_task is not None else 0))

    @property
    def completed_since_report(self) -> int:
        return self._completed_since_report

    @property
    def settled(self) -> bool:
        """Idle: nothing queued, nothing playing, no round to report."""
        return (
            not self._tasks
            and self._playing_task is None
            and not (self._completed_since_report or self._started_since_report)
        )

    def take_round_counts(self) -> int:
        """Pull and reset the per-window counters for a queue_finished frame."""
        completed = self._completed_since_report
        self._completed_since_report = 0
        self._started_since_report = 0
        return completed

    async def shutdown(self) -> None:
        for worker in (self._playback_worker, self._synth_worker):
            if worker is not None:
                worker.cancel()
        if self._synth is not None:
            self._synth.cancel()
        for worker in (self._playback_worker, self._synth_worker):
            if worker is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await worker

    # -- workers ---------------------------------------------------------------

    async def _tick_wait(self) -> None:
        # No cross-task wakeup primitive: every wait re-checks its predicates
        # each tick, so a sync caller (submit/interrupt) never needs a lock.
        await asyncio.sleep(TICK_SECONDS)

    def _ensure_workers(self) -> None:
        if self._synth_worker is None or self._synth_worker.done():
            self._synth_worker = asyncio.create_task(self._synth_loop())
        if self._playback_worker is None or self._playback_worker.done():
            self._playback_worker = asyncio.create_task(self._playback_loop())

    async def _synth_loop(self) -> None:
        """Serialize all synthesis in queue order, far ahead of playback."""
        while True:
            try:
                target, text = await self._await_synth_target()
                self._synth = _Synth(self._engine, text, target)
                await self._synth.wait()
                self._synth = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self._synth = None
                LOGGER.exception("synth worker iteration failed")

    async def _await_synth_target(self) -> tuple[PlaybackTask, str]:
        while True:
            for task in self._tasks:
                text = task.unit.speech
                if task.audible and text is not None and not task.synth_started:
                    task.synth_started = True
                    return task, text
            await self._tick_wait()

    async def _playback_loop(self) -> None:
        while True:
            try:
                task = await self._await_head()
                await self._perform(task)
                if not self._tasks and (
                    self._completed_since_report or self._started_since_report
                ):
                    await self._report_drain()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("playback worker iteration failed")

    async def _await_head(self) -> PlaybackTask:
        while not self._tasks:
            await self._tick_wait()
        return self._tasks[0]

    async def _report_drain(self) -> None:
        await self._on_drain()

    # -- performing one task -----------------------------------------------------

    async def _perform(self, task: PlaybackTask) -> None:
        if not task.audible:
            await self._apply_visual(task)
            self._retire(task, counted=True)
            return
        while True:
            if task.dropped:
                return  # stop already removed it from the queue
            if task.error is not None:
                await self._on_error(f"speech synthesis failed: {task.error}")
                if self._mouth_active:  # a takeover cut left the mouth open
                    await self._set_mouth(False)
                self._retire(task, counted=False)
                return
            if task.play_ready:
                break
            await self._tick_wait()
        await self._play_speech(task)

    async def _play_speech(self, task: PlaybackTask) -> None:
        text = task.unit.subtitle_text
        await self._hold_gap(task)
        if task.dropped:
            return
        # Claim the head BEFORE any await below: a stop landing during the
        # presentation sends must find this task playing, not droppable.
        self._playing_task = task
        started_at = self._player.written_seconds
        cursor = len(task.audio)
        if cursor:
            self._player.enqueue(bytes(task.audio[:cursor]))
        await self._start_present(task, text)
        self._mouth_active = True
        target_written: float | None = None
        error_reported = False
        try:
            while True:
                if self._takeover_pending():
                    self._player.clear()
                    LOGGER.info(
                        "seamless takeover: cut %r after %.1fs of audio",
                        text,
                        self._player.written_seconds - started_at,
                    )
                    self._retire(task, counted=True)
                    return
                ready = len(task.audio)
                if ready > cursor:
                    self._player.enqueue(bytes(task.audio[cursor:ready]))
                    cursor = ready
                if target_written is None and (
                    task.synth_done or task.error is not None
                ):
                    target_written = started_at + cursor / BYTES_PER_SECOND
                    if task.error is not None and not error_reported:
                        error_reported = True
                        await self._on_error(
                            f"speech synthesis failed mid-sentence: {task.error}"
                        )
                duration_ms = (
                    estimate_duration_ms(text, cursor)
                    if target_written is not None
                    else 0
                )
                await self._reveal(text, started_at, duration_ms)
                if (
                    target_written is not None
                    and self._player.written_seconds + 0.005 >= (target_written)
                ):
                    break
                await self._tick_wait()
            await self._reveal(
                text, started_at, estimate_duration_ms(text, cursor), full=True
            )
            await self._set_mouth(False)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("speech playback failed: %r", text)
        self._retire(task, counted=True)
        self._last_speech_end_at = time.monotonic()

    async def _hold_gap(self, task: PlaybackTask) -> None:
        """Minimum breathing gap since the previous sentence fully played.

        The synthesis wait counts into the gap (it is a minimum interval, not
        an added delay); an armed seamless takeover skips it entirely.
        """
        if self._sentence_gap <= 0.0 or self._last_speech_end_at is None:
            return
        deadline = self._last_speech_end_at + self._sentence_gap
        while time.monotonic() < deadline:
            if task.dropped or self._takeover_pending():
                return
            await self._tick_wait()

    def _takeover_pending(self) -> bool:
        """True when a post-stop task's audio is ready to cut the playing head."""
        if not self._preempt_armed or len(self._tasks) < 2:
            return False
        nxt = self._tasks[1]
        if not nxt.audible or nxt.error is not None:
            return False
        return nxt.play_ready

    async def _start_present(self, task: PlaybackTask, text: str) -> None:
        """FIRE EVERY VISIBLE EVENT at start of play (ADR-0008): the moment the
        first audio byte reached the player, never before."""
        if task.unit.ppt is not None:
            await self._apply_ppt(task.unit.ppt)
        for tag, value in task.unit.visual_tags().items():
            await self._emit({"type": tag, "tag": value})
        await self._emit({"type": "subtitle_set", "text": text, "visible_chars": 0})
        await self._set_mouth(True)
        self._started_since_report += 1

    async def _apply_visual(self, task: PlaybackTask) -> None:
        """Visual-only head: its start of play is the moment it becomes head."""
        if task.unit.ppt is not None:
            await self._apply_ppt(task.unit.ppt)
        for tag, value in task.unit.visual_tags().items():
            await self._emit({"type": tag, "tag": value})
        self._started_since_report += 1

    def _retire(self, task: PlaybackTask, *, counted: bool) -> None:
        if self._tasks and self._tasks[0] is task:
            self._tasks.pop(0)
        if counted:
            self._completed_since_report += 1
        if self._playing_task is task:
            self._playing_task = None
        if self._preempt_armed:
            # The pre-stop head left the queue (played out, cut, or failed);
            # everything left plays in order — takeover was just for that one.
            self._preempt_armed = False

    async def _set_mouth(self, active: bool) -> None:
        await self._emit({"type": "speech_state", "active": active})
        self._mouth_active = active

    async def _reveal(
        self, text: str, started_at: float, duration_ms: int, *, full: bool = False
    ) -> None:
        elapsed_ms = int(max(0.0, self._player.written_seconds - started_at) * 1000)
        visible = (
            len(text) if full else reveal_visible_chars(text, elapsed_ms, duration_ms)
        )
        await self._emit(
            {"type": "subtitle_set", "text": text, "visible_chars": visible}
        )


__all__ = ["TICK_SECONDS", "PlaybackQueue", "PlaybackTask"]
