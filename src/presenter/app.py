"""PresenterApp: the expression pipeline of the aggregate output module.

Wires everything together (ADR-0002/0003/0004):
units accepted from the hub enter the bounded expression queue; a round worker
plays them in list order (visual tags -> bridge, speech -> prefetched TTS ->
player), reveals subtitles on the real playback clock (linear reveal), and
after the queue drains (or after a control unit clears it) sends the
queue_finished input frame that keeps the Agent's Invocation loop alive.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from presenter.config import ConfigError, Settings
from presenter.hub_client import PresenterClient
from presenter.playback import PlayerSink
from presenter.presentation import GodotSupervisor, PresentationBridge
from presenter.subtitles import estimate_duration_ms, reveal_visible_chars
from presenter.tts import BYTES_PER_SECOND, TtsEngine, TtsError
from presenter.units import ExpressionUnit, UnitError, parse_unit

LOGGER = logging.getLogger(__name__)

_TICK = 0.05  # subtitle ticker step, seconds
_SYNTH_DONE = object()  # end-of-sentence marker in the audio chunk queue


def build_engine(settings: Settings) -> TtsEngine:
    """Construct the primary TTS engine (cosyvoice; audio_cpp arrives in M4)."""
    from presenter.cosyvoice import CosyVoiceEngine

    if not settings.dashscope_api_key:
        raise ConfigError("PRESENTER_DASHSCOPE_API_KEY", "required for the cosyvoice engine")
    if not settings.cosyvoice_voice:
        raise ConfigError("PRESENTER_COSYVOICE_VOICE", "cloned voice id is required")
    return CosyVoiceEngine(
        api_key=settings.dashscope_api_key,
        model=settings.cosyvoice_model,
        voice=settings.cosyvoice_voice,
    )


class _Synth:
    """One sentence synthesized ahead of playback into an async chunk queue."""

    def __init__(self, engine: TtsEngine, text: str) -> None:
        self.chunks: asyncio.Queue[bytes | BaseException | object] = asyncio.Queue()
        self._task = asyncio.create_task(self._pump(engine, text))

    async def _pump(self, engine: TtsEngine, text: str) -> None:
        try:
            async for chunk in engine.stream(text):
                await self.chunks.put(chunk)
        except asyncio.CancelledError:
            await self.chunks.put(asyncio.CancelledError())
            raise
        except Exception as exc:
            await self.chunks.put(exc)
        await self.chunks.put(_SYNTH_DONE)

    def cancel(self) -> None:
        self._task.cancel()


class PresenterApp:
    def __init__(
        self,
        settings: Settings,
        engine: TtsEngine,
        player: PlayerSink,
        bridge: PresentationBridge,
        supervisor: GodotSupervisor | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._player = player
        self._bridge = bridge
        self._supervisor = supervisor
        self._queue: asyncio.Queue[ExpressionUnit] = asyncio.Queue(
            maxsize=settings.queue_max
        )
        self._client: PresenterClient | None = None
        self._worker: asyncio.Task[None] | None = None
        self._current_synth: _Synth | None = None
        self._round_completed = 0
        self._round_active = False
        self._stop_requested = False
        self._page: int | None = None  # real deck state lands in M3

    # -- hub-facing callbacks ---------------------------------------------

    async def on_unit(self, payload: dict[str, Any]) -> tuple[str, str]:
        """Accept one dispatched unit; return (result status, detail)."""
        try:
            unit = parse_unit(payload)
        except UnitError as exc:
            return "error", f"invalid output unit: {exc}"
        if unit.is_stop:
            dropped = self._clear_queue()
            interrupted = self._round_active or dropped > 0
            LOGGER.info("control unit stop: dropped %d queued unit(s)", dropped)
            if interrupted:
                await self._emit_queue_finished(interrupted=True)
            return "ok", f"stopped, dropped {dropped} queued unit(s)"
        try:
            self._queue.put_nowait(unit)
        except asyncio.QueueFull:
            return "error", "expression queue full"
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_round())
        return "ok", "queued"

    def status(self) -> dict[str, Any]:
        return {
            "tts_engine": self._engine.name,
            "playing": self._round_active,
            "queue_depth": self._queue.qsize(),
            "completed_sentences": self._round_completed,
            "presentation_available": self._bridge.available,
            "page": self._page,
        }

    async def on_user_event(self, kind: str, detail: str) -> None:
        """Bridge user events from the presentation process into input frames."""
        if self._client is None:
            LOGGER.debug("user event while offline, dropping: %s", kind)
            return
        description = f"观众在前端执行了 {kind}"
        if detail:
            description = f"观众在前端执行了 {kind}：{detail}"
        await self._client.send_input(
            description, metadata={"event": "user_interaction", "kind": kind, "detail": detail}
        )

    # -- round execution ----------------------------------------------------

    async def _run_round(self) -> None:
        self._round_active = True
        self._stop_requested = False
        self._round_completed = 0
        lookahead: ExpressionUnit | None = None
        synth_next: _Synth | None = None
        try:
            while True:
                unit = lookahead or self._get_queued()
                lookahead = None
                if unit is None:
                    break
                for tag, value in unit.visual_tags().items():
                    await self._bridge.send({"type": tag, "tag": value})
                if unit.speech is not None:
                    synth = synth_next or _Synth(self._engine, unit.speech)
                    synth_next = None
                    lookahead = self._get_queued()
                    if (
                        lookahead is not None
                        and not lookahead.is_stop
                        and lookahead.speech is not None
                    ):
                        synth_next = _Synth(self._engine, lookahead.speech)
                    await self._speak(unit, synth)
                    if self._stop_requested:
                        break
                self._round_completed += 1
            if not self._stop_requested:
                while not self._player.idle():
                    await asyncio.sleep(_TICK)
                await self._emit_queue_finished(interrupted=False)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("round worker failed")
            await self._report_failure(exc)
        finally:
            if synth_next is not None:
                synth_next.cancel()
            self._round_active = False
            if self._worker is asyncio.current_task():
                self._worker = None

    async def _speak(self, unit: ExpressionUnit, synth: _Synth) -> None:
        text = unit.subtitle_text
        self._current_synth = synth
        await self._bridge.send({"type": "subtitle_set", "text": text, "visible_chars": 0})
        started_at = self._player.written_seconds
        total_bytes = 0
        duration_ms = 0
        # Drain synthesized audio while it plays, revealing subtitles on the
        # playback clock; duration becomes exact when synthesis completes.
        while True:
            item = await synth.chunks.get()
            if isinstance(item, asyncio.CancelledError):
                raise TtsError("synthesis cancelled by stop control")
            if isinstance(item, BaseException):
                raise TtsError(f"synthesis failed: {item}")
            if not isinstance(item, bytes):
                if item is _SYNTH_DONE:
                    duration_ms = estimate_duration_ms(text, total_bytes)
                    break
                continue
            total_bytes += len(item)
            self._player.enqueue(item)
            if total_bytes % 16000 < len(item):
                await self._reveal(text, started_at, duration_ms)
        await self._reveal(text, started_at, duration_ms)
        # Wait for the enqueued audio to actually reach the output: the target
        # clock position is tied to the enqueued byte count, with a 5 ms
        # tolerance so accumulated float time can always cross the line.
        target_written = started_at + total_bytes / BYTES_PER_SECOND
        while (
            self._player.written_seconds + 0.005 < target_written
            and not self._stop_requested
        ):
            await self._reveal(text, started_at, duration_ms)
            await asyncio.sleep(_TICK)
        await self._reveal(text, started_at, duration_ms, full=True)
        self._current_synth = None

    async def _reveal(self, text: str, started_at: float, duration_ms: int, *, full: bool = False) -> None:
        elapsed_ms = int(max(0.0, self._player.written_seconds - started_at) * 1000)
        visible = len(text) if full else reveal_visible_chars(text, elapsed_ms, duration_ms)
        await self._bridge.send({"type": "subtitle_set", "text": text, "visible_chars": visible})

    def _get_queued(self) -> ExpressionUnit | None:
        with contextlib.suppress(asyncio.QueueEmpty):
            return self._queue.get_nowait()
        return None

    def _clear_queue(self) -> int:
        self._stop_requested = True
        if self._worker is not None:
            self._worker.cancel()
            self._worker = None
        if self._current_synth is not None:
            self._current_synth.cancel()
            self._current_synth = None
        dropped = 0
        while self._get_queued() is not None:
            dropped += 1
        self._player.clear()
        return dropped

    # -- input frames ---------------------------------------------------------

    async def _emit_queue_finished(self, *, interrupted: bool) -> None:
        completed = self._round_completed
        self._round_completed = 0
        await self._bridge.send({"type": "subtitle_clear"})
        if self._client is None:
            return
        if interrupted:
            content = f"树莓娘的当前表达被打断清空，本轮已完成 {completed} 句。"
        else:
            content = f"树莓娘已完整播完本轮表达，共 {completed} 句。"
        await self._client.send_input(
            content,
            metadata={
                "event": "queue_finished",
                "interrupted": interrupted,
                "completed_sentences": completed,
                "page": self._page,
            },
        )

    async def _report_failure(self, exc: Exception) -> None:
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            await self._client.send_result("error", f"expression failed: {exc}")

    # -- lifecycle -------------------------------------------------------------

    async def run_forever(self) -> None:
        if self._supervisor is not None:
            self._supervisor.start()
        delay = 1.0
        while True:
            try:
                async with PresenterClient(
                    hub_url=self._settings.hub_url,
                    token=self._settings.hub_token,
                    on_unit=self.on_unit,
                    status_provider=self.status,
                    tls_ca_path=self._settings.tls_ca_path,
                ) as client:
                    self._client = client
                    delay = 1.0
                    await client.wait_closed()
            except Exception as exc:
                LOGGER.warning("hub connection lost: %s (retry in %.0fs)", exc, delay)
            finally:
                self._client = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


__all__ = ["PresenterApp", "build_engine"]
