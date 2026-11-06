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
import time
from pathlib import Path
from typing import Any

from presenter.config import ConfigError, Settings
from presenter.decks import DeckError, DeckManager
from presenter.hub_client import PresenterClient
from presenter.playback import PlayerSink
from presenter.presentation import BridgeSink, GodotSupervisor
from presenter.subtitles import estimate_duration_ms, reveal_visible_chars
from presenter.tts import BYTES_PER_SECOND, TtsEngine, TtsError
from presenter.units import ExpressionUnit, PptCommand, UnitError, parse_unit

LOGGER = logging.getLogger(__name__)

_TICK = 0.05  # subtitle ticker step, seconds
_SYNTH_DONE = object()  # end-of-sentence marker in the audio chunk queue

# Page scripts ride every queue_finished input (ADR-0006); bound their token
# cost per invocation regardless of what the deck manifest stores.
SCRIPT_BUDGET_CHARS = 1600


def _bounded_script(script: str | None) -> str | None:
    if not script:
        return None
    if len(script) <= SCRIPT_BUDGET_CHARS:
        return script
    return script[:SCRIPT_BUDGET_CHARS] + "…（讲稿截断）"


def build_engine(settings: Settings) -> TtsEngine:
    """Assemble the TTS engine per PRESENTER_TTS_ENGINE (ADR-0001).

    auto: cloud CosyVoice as primary with the audio.cpp fallback engaged
    automatically on repeated cloud failures; cosyvoice / audio_cpp force one
    engine with no degradation.
    """
    if settings.tts_engine == "cosyvoice":
        return _cosyvoice_engine(settings)
    if settings.tts_engine == "audio_cpp":
        return _audio_cpp_engine(settings)
    primary = _cosyvoice_engine(settings) if _cosyvoice_configured(settings) else None
    fallback = _audio_cpp_engine(settings) if _audio_cpp_configured(settings) else None
    if primary is not None and fallback is not None:
        from presenter.fallback import AutoTtsEngine

        return AutoTtsEngine(
            primary,
            fallback,
            fail_after=settings.tts_fallback_after,
            retry_after_seconds=settings.tts_retry_after_seconds,
        )
    if primary is not None:
        return primary
    if fallback is not None:
        return fallback
    raise ConfigError(
        "PRESENTER_TTS_ENGINE",
        "auto needs cosyvoice (API key + voice) or audio_cpp (model + voice ref)",
    )


def _cosyvoice_configured(settings: Settings) -> bool:
    return bool(settings.dashscope_api_key and settings.cosyvoice_voice)


def _audio_cpp_configured(settings: Settings) -> bool:
    return bool(settings.audio_cpp_model and settings.audio_cpp_voice_ref)


def _cosyvoice_engine(settings: Settings) -> TtsEngine:
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


def _audio_cpp_engine(settings: Settings) -> TtsEngine:
    from presenter.audiocpp import AudioCppEngine

    if not settings.audio_cpp_model:
        raise ConfigError("PRESENTER_AUDIO_CPP_MODEL", "server-configured model id is required")
    if not settings.audio_cpp_voice_ref:
        raise ConfigError(
            "PRESENTER_AUDIO_CPP_VOICE_REF", "cloning reference WAV path is required"
        )
    return AudioCppEngine(
        base_url=settings.audio_cpp_url,
        model=settings.audio_cpp_model,
        voice_ref=settings.audio_cpp_voice_ref,
        reference_text=settings.audio_cpp_reference_text,
        sample_rate=settings.audio_cpp_sample_rate,
    )


class _Synth:
    """One sentence synthesized ahead of playback into an async chunk queue."""

    def __init__(self, engine: TtsEngine, text: str) -> None:
        LOGGER.debug("tts request (engine=%s): %r", engine.name, text)
        self._started_at = time.monotonic()
        self._first_chunk_at: float | None = None
        self.total_bytes = 0
        self.chunks: asyncio.Queue[bytes | BaseException | object] = asyncio.Queue()
        self._task = asyncio.create_task(self._pump(engine, text))

    async def _pump(self, engine: TtsEngine, text: str) -> None:
        try:
            async for chunk in engine.stream(text):
                if self._first_chunk_at is None:
                    self._first_chunk_at = time.monotonic()
                self.total_bytes += len(chunk)
                await self.chunks.put(chunk)
        except asyncio.CancelledError:
            await self.chunks.put(asyncio.CancelledError())
            raise
        except Exception as exc:
            LOGGER.warning(
                "tts synthesis failed after %.2fs (%s): %s",
                time.monotonic() - self._started_at,
                engine.name,
                exc,
            )
            await self.chunks.put(exc)
            return
        LOGGER.debug(
            "tts synthesized %d bytes of %s audio in %.2fs (first chunk %.2fs): %r",
            self.total_bytes,
            engine.name,
            time.monotonic() - self._started_at,
            (self._first_chunk_at or self._started_at) - self._started_at,
            text,
        )
        await self.chunks.put(_SYNTH_DONE)

    def cancel(self) -> None:
        self._task.cancel()


class PresenterApp:
    def __init__(
        self,
        settings: Settings,
        engine: TtsEngine,
        player: PlayerSink,
        bridge: BridgeSink,
        supervisor: GodotSupervisor | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._player = player
        self._bridge = bridge
        self._supervisor = supervisor
        self._decks = DeckManager(Path(settings.decks_dir)) if settings.decks_dir else None
        self._queue: asyncio.Queue[ExpressionUnit] = asyncio.Queue(
            maxsize=settings.queue_max
        )
        self._client: PresenterClient | None = None
        self._worker: asyncio.Task[None] | None = None
        self._current_synth: _Synth | None = None
        self._round_completed = 0
        self._round_active = False
        self._stop_requested = False

    # -- hub-facing callbacks ---------------------------------------------

    async def on_unit(self, payload: dict[str, Any]) -> tuple[str, str]:
        """Accept one dispatched unit; return (result status, detail)."""
        LOGGER.debug("unit accepted from hub: %r", payload)
        try:
            unit = parse_unit(payload)
        except UnitError as exc:
            LOGGER.warning("invalid output unit rejected: %s", exc)
            return "error", f"invalid output unit: {exc}"
        if unit.is_stop:
            dropped = self._clear_queue()
            interrupted = self._round_active or dropped > 0
            await self._bridge.send({"type": "speech_state", "active": False})
            LOGGER.info("control unit stop: dropped %d queued unit(s)", dropped)
            if interrupted:
                await self._emit_queue_finished(interrupted=True)
            return "ok", f"stopped, dropped {dropped} queued unit(s)"
        try:
            self._queue.put_nowait(unit)
        except asyncio.QueueFull:
            LOGGER.warning("expression queue full (%d), unit rejected", self._settings.queue_max)
            return "error", "expression queue full"
        LOGGER.debug("unit queued (depth=%d/%d)", self._queue.qsize(), self._settings.queue_max)
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run_round())
        return "ok", "queued"

    def status(self) -> dict[str, Any]:
        deck = self._decks.snapshot() if self._decks else None
        return {
            "tts_engine": self._engine.name,
            "playing": self._round_active,
            "queue_depth": self._queue.qsize(),
            "completed_sentences": self._round_completed,
            "presentation_available": self._bridge.available,
            "deck": deck.deck if deck else None,
            "page": deck.page if deck else None,
            "total_pages": deck.total_pages if deck else None,
            "page_script": _bounded_script(deck.script) if deck else None,
        }

    # -- round execution ----------------------------------------------------

    async def _run_round(self) -> None:
        self._round_active = True
        self._stop_requested = False
        self._round_completed = 0
        LOGGER.debug("round worker started (queued=%d)", self._queue.qsize())
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
                if unit.ppt is not None:
                    await self._apply_ppt(unit.ppt)
                if unit.speech is not None:
                    synth = synth_next or _Synth(self._engine, unit.speech)
                    synth_next = None
                    lookahead, synth_next = await self._speak(
                        unit, synth, lookahead, synth_next
                    )
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

    async def _apply_ppt(self, command: PptCommand) -> None:
        """Execute a deck instruction; failure is an async error result, non-fatal."""
        if self._decks is None:
            await self._report_async_error(
                f"ppt {command.action} failed: PRESENTER_DECKS_DIR is not configured"
            )
            return
        try:
            slide = self._decks.apply(command)
        except DeckError as exc:
            await self._report_async_error(f"ppt {command.action} failed: {exc}")
            return
        await self._bridge.send(slide)

    def _presynth_next(
        self, lookahead: ExpressionUnit | None, synth_next: _Synth | None
    ) -> tuple[ExpressionUnit | None, _Synth | None]:
        """Retry the lookahead once per call; idempotent while the slot is filled.

        The hub dispatches units one by one and the next unit can land slightly
        after the current sentence started playing; a one-shot lookahead would
        miss it forever and the next sentence would only start synthesizing
        once this one finished playing (a measured 6-8 s silent gap).  Callers
        retry this from every synthesis/playback wait tick.
        """
        if lookahead is None:
            lookahead = self._get_queued()
        if (
            lookahead is not None
            and synth_next is None
            and not lookahead.is_stop
            and lookahead.speech is not None
        ):
            synth_next = _Synth(self._engine, lookahead.speech)
        return lookahead, synth_next

    async def _speak(
        self,
        unit: ExpressionUnit,
        synth: _Synth,
        lookahead: ExpressionUnit | None,
        synth_next: _Synth | None,
    ) -> tuple[ExpressionUnit | None, _Synth | None]:
        """Play one sentence; returns the lookahead pair for the next round step."""
        text = unit.subtitle_text
        self._current_synth = synth
        started_at = self._player.written_seconds
        total_bytes = 0
        duration_ms = 0
        speaking = False
        try:
            # Drain synthesized audio while it plays, revealing subtitles on the
            # playback clock; duration becomes exact when synthesis completes.
            # Subtitle and speaking state wait for the FIRST audio chunk: the
            # screen keeps the previous sentence for the whole first-chunk wait
            # (measured 3-8 s) instead of going blank.
            while True:
                lookahead, synth_next = self._presynth_next(lookahead, synth_next)
                try:
                    item = await asyncio.wait_for(synth.chunks.get(), timeout=_TICK)
                except TimeoutError:
                    continue
                if isinstance(item, asyncio.CancelledError):
                    raise TtsError("synthesis cancelled by stop control")
                if isinstance(item, BaseException):
                    raise TtsError(f"synthesis failed: {item}")
                if not isinstance(item, bytes):
                    if item is _SYNTH_DONE:
                        duration_ms = estimate_duration_ms(text, total_bytes)
                        break
                    continue
                if not speaking:
                    speaking = True
                    await self._bridge.send(
                        {"type": "subtitle_set", "text": text, "visible_chars": 0}
                    )
                    await self._bridge.send({"type": "speech_state", "active": True})
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
                lookahead, synth_next = self._presynth_next(lookahead, synth_next)
                await self._reveal(text, started_at, duration_ms)
                await asyncio.sleep(_TICK)
            await self._reveal(text, started_at, duration_ms, full=True)
            if speaking:
                await self._bridge.send({"type": "speech_state", "active": False})
            LOGGER.info(
                "sentence fully played (%.1fs audio): %r", total_bytes / BYTES_PER_SECOND, text
            )
        except BaseException:
            if synth_next is not None:
                synth_next.cancel()
            raise
        finally:
            self._current_synth = None
        return lookahead, synth_next

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
        # 字幕在轮次结束后保留，直到下一条 subtitle_set 开始揭示才替换
        # （观众应能继续读完最后一句）；subtitle_clear 仅供 deck 等显式清空场景。
        completed = self._round_completed
        self._round_completed = 0
        LOGGER.info("round finished (sentences=%d, interrupted=%s)", completed, interrupted)
        if self._client is None:
            return
        if interrupted:
            content = f"树莓娘的当前表达被打断清空，本轮已完成 {completed} 句。"
        else:
            content = f"树莓娘已完整播完本轮表达，共 {completed} 句。"
        deck = self._decks.snapshot() if self._decks else None
        if deck and deck.deck is not None:
            # 无视觉的 Agent 靠这段讲稿知道屏上这页在讲什么（ADR-0006）。
            content += (
                f"当前幻灯片 {deck.deck} 第 {deck.page}/{deck.total_pages} 页。"
            )
            script = _bounded_script(deck.script)
            if script:
                content += f"本页讲稿：{script}"
        await self._client.send_input(
            content,
            metadata={
                "event": "queue_finished",
                "interrupted": interrupted,
                "completed_sentences": completed,
                "deck": deck.deck if deck else None,
                "page": deck.page if deck else None,
            },
        )

    async def _report_async_error(self, detail: str) -> None:
        """Report a non-fatal execution failure back to the hub as a result."""
        LOGGER.warning("async error result: %s", detail)
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            await self._client.send_result("error", detail)

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
