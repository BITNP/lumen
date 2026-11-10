"""PresenterApp: the expression pipeline of the aggregate output module.

Wires everything together around the unified playback queue (ADR-0002/0003/
0008): units accepted from the hub become playback tasks; the queue plays the
head while a background worker pre-synthesizes the rest in order, reveals
subtitles on the real playback clock, and reports queue_finished when the
queue truly drains (or its interrupted variant right after a control stop
dropped pending tasks).  Timing, cancellation and takeover semantics all live
in presenter.playqueue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from presenter.config import ConfigError, Settings
from presenter.decks import DeckError, DeckManager
from presenter.hub_client import PresenterClient
from presenter.playback import PlayerSink
from presenter.playqueue import PlaybackQueue
from presenter.presentation import BridgeSink, GodotSupervisor
from presenter.tts import TtsEngine
from presenter.units import PptCommand, UnitError, parse_unit

LOGGER = logging.getLogger(__name__)

# Page texts ride every queue_finished input (ADR-0006/0007); bound their
# token cost per invocation regardless of what the deck manifest stores.
PAGE_TEXT_BUDGET_CHARS = 1600


def _bounded_page_text(text: str | None) -> str | None:
    if not text:
        return None
    if len(text) <= PAGE_TEXT_BUDGET_CHARS:
        return text
    return text[:PAGE_TEXT_BUDGET_CHARS] + "…（已截断）"


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
        raise ConfigError(
            "PRESENTER_DASHSCOPE_API_KEY", "required for the cosyvoice engine"
        )
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
        raise ConfigError(
            "PRESENTER_AUDIO_CPP_MODEL", "server-configured model id is required"
        )
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
        self._decks = (
            DeckManager(Path(settings.decks_dir)) if settings.decks_dir else None
        )
        self._playback = PlaybackQueue(
            engine=engine,
            player=player,
            emit=bridge.send,
            apply_ppt=self._apply_ppt,
            on_drain=self._on_drain,
            on_error=self._report_async_error,
            sentence_gap_seconds=settings.sentence_gap_seconds,
        )
        self._client: PresenterClient | None = None

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
            dropped = self._playback.interrupt()
            LOGGER.info("control unit stop: dropped %d queued unit(s)", dropped)
            if dropped > 0:
                # The cleared round reports itself at once (ADR-0008); the
                # surviving head + post-stop round still report their own
                # drain later — the heartbeat must never be swallowed.
                await self._emit_queue_finished(
                    interrupted=True, completed=self._playback.take_round_counts()
                )
            return "ok", f"stopped, dropped {dropped} queued unit(s)"
        if not self._playback.can_accept(self._settings.queue_max):
            LOGGER.warning(
                "playback queue full (%d), unit rejected", self._settings.queue_max
            )
            return "error", "playback queue full"
        self._playback.submit(unit)
        LOGGER.debug(
            "unit queued (depth=%d/%d)",
            self._playback.pending_depth + 1,
            self._settings.queue_max,
        )
        return "ok", "queued"

    def status(self) -> dict[str, Any]:
        deck = self._decks.snapshot() if self._decks else None
        return {
            "tts_engine": self._engine.name,
            "playing": self._playback.playing,
            "queue_depth": self._playback.pending_depth,
            "completed_sentences": self._playback.completed_since_report,
            "presentation_available": self._bridge.available,
            "deck": deck.deck if deck else None,
            "page": deck.page if deck else None,
            "total_pages": deck.total_pages if deck else None,
            "page_script": _bounded_page_text(deck.script) if deck else None,
            "page_summary": _bounded_page_text(deck.summary) if deck else None,
        }

    # -- playback queue callbacks ---------------------------------------------

    async def _apply_ppt(self, command: PptCommand) -> None:
        """Execute a deck instruction at its unit's start of play; failure is
        an async error result, non-fatal (the sentence keeps going)."""
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

    async def _on_drain(self) -> None:
        await self._emit_queue_finished(
            interrupted=False, completed=self._playback.take_round_counts()
        )

    # -- input frames ---------------------------------------------------------

    async def _emit_queue_finished(self, *, interrupted: bool, completed: int) -> None:
        # 字幕在轮次结束后保留，直到下一条 subtitle_set 开始揭示才替换
        # （观众应能继续读完最后一句）；subtitle_clear 仅供 deck 等显式清空场景。
        LOGGER.info(
            "queue finished (sentences=%d, interrupted=%s)", completed, interrupted
        )
        if self._client is None:
            return
        if interrupted:
            content = f"树莓娘的当前表达被打断清空，本轮已完成 {completed} 句。"
        else:
            content = f"树莓娘已完整播完本轮表达，共 {completed} 句。"
        deck = self._decks.snapshot() if self._decks else None
        if deck and deck.deck is not None:
            # 无视觉的 Agent 靠这两段文本知道屏上这页讲了什么、该怎么讲
            # （ADR-0006/0007：内容与讲稿分层，人工只改讲稿不碰内容）。
            content += f"当前幻灯片 {deck.deck} 第 {deck.page}/{deck.total_pages} 页。"
            summary = _bounded_page_text(deck.summary)
            if summary:
                content += f"\n本页内容：{summary}"
            script = _bounded_page_text(deck.script)
            if script:
                content += f"\n建议讲稿：{script}"
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
            except Exception as exc:  # noqa: BLE001 - 重连循环最后防线：任何断线形态都回到等待重连
                LOGGER.warning("hub connection lost: %s (retry in %.0fs)", exc, delay)
            finally:
                self._client = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def shutdown(self) -> None:
        await self._playback.shutdown()


__all__ = ["PresenterApp", "build_engine"]
