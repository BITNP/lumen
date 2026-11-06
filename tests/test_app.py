"""Expression pipeline: order, accept results, queue overflow, stop control.

Uses tests/placeholders (silent player, fake engine, recording client); the
real synthesis path is covered by the real-marked smoke test.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from placeholders import (
    FakeEngine,
    RecordingBridge,
    RecordingClient,
    SilentPlayer,
    write_deck,
)
from presenter.app import PresenterApp
from presenter.config import load_settings
from presenter.presentation import PresentationBridge
from presenter.tts import TtsEngine


def _app(
    *,
    engine: TtsEngine | None = None,
    queue_max: int = 64,
    bridge: PresentationBridge | None = None,
    decks_dir: object = None,
) -> PresenterApp:
    env = {"WAYCAST_HUB_URL": "ws://127.0.0.1:1", "PRESENTER_QUEUE_MAX": str(queue_max)}
    if decks_dir is not None:
        env["PRESENTER_DECKS_DIR"] = str(decks_dir)
    settings = load_settings(env)
    app = PresenterApp(
        settings, engine or FakeEngine(), SilentPlayer(), bridge or PresentationBridge(port=0)
    )
    app._client = RecordingClient()  # type: ignore[assignment]
    return app


async def _settled(app: PresenterApp, *, seconds: float = 2.0) -> None:
    """Wait until the round worker created by on_unit has finished its round."""
    assert app._worker is not None, "no round worker was started"
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if app._worker is None:
            await asyncio.sleep(0.02)  # let the queue_finished send settle
            return
        await asyncio.sleep(0.02)
    raise AssertionError("round did not finish")


def test_two_sentences_play_in_order_then_queue_finished() -> None:
    async def scenario() -> PresenterApp:
        app = _app()
        assert await app.on_unit({"speech": "第一句"}) == ("ok", "queued")
        assert await app.on_unit({"speech": "第二句"}) == ("ok", "queued")
        await _settled(app)
        return app

    app = asyncio.run(scenario())
    engine: FakeEngine = app._engine  # type: ignore[assignment]
    client: RecordingClient = app._client  # type: ignore[assignment]
    assert engine.calls == ["第一句", "第二句"]
    assert len(client.inputs) == 1
    finished = client.inputs[0]["metadata"]
    assert finished["event"] == "queue_finished"
    assert finished["interrupted"] is False
    assert finished["completed_sentences"] == 2


def test_subtitle_stays_after_round_ends() -> None:
    """ADR-0010 表现语义：轮次结束不清空字幕，直到下一条字幕开始替换。"""

    async def scenario() -> RecordingBridge:
        bridge = RecordingBridge()
        app = _app(bridge=bridge)
        assert await app.on_unit({"speech": "播完保留的字幕"}) == ("ok", "queued")
        await _settled(app)
        return bridge

    bridge = asyncio.run(scenario())
    subtitles = [c for c in bridge.commands if c["type"] in ("subtitle_set", "subtitle_clear")]
    assert subtitles, "sentence playback should drive subtitle messages"
    assert all(c["type"] == "subtitle_set" for c in subtitles), "round end must not clear the subtitle"
    final = subtitles[-1]
    assert final["text"] == "播完保留的字幕"
    assert final["visible_chars"] == len(final["text"])


def test_visual_only_units_finish_without_synthesis() -> None:
    async def scenario() -> RecordingClient:
        app = _app()
        await app.on_unit({"emotion": "happy"})
        await app.on_unit({"motion": "wave"})
        await _settled(app)
        return app._client  # type: ignore[return-value]

    client = asyncio.run(scenario())
    assert client.inputs[0]["metadata"]["completed_sentences"] == 2


def test_queue_overflow_returns_error_result() -> None:
    async def scenario() -> tuple[PresenterApp, list[tuple[str, str]]]:
        app = _app(queue_max=2)
        results = [await app.on_unit({"emotion": "happy"}) for _ in range(3)]
        await _settled(app)
        return app, results

    app, results = asyncio.run(scenario())
    assert results[0][0] == results[1][0] == "ok"
    assert results[2] == ("error", "expression queue full")
    client: RecordingClient = app._client  # type: ignore[assignment]
    assert client.inputs[0]["metadata"]["completed_sentences"] == 2


def test_invalid_unit_is_rejected_without_queueing() -> None:
    async def scenario() -> tuple[tuple[str, str], tuple[str, str]]:
        app = _app()
        bad = await app.on_unit({"control": "replace"})
        empty = await app.on_unit({"speech": "  "})
        assert app._worker is None, "rejected units must not start a round"
        return bad, empty

    bad, empty = asyncio.run(scenario())
    assert bad[0] == "error" and "control" in bad[1]
    assert empty[0] == "error" and empty[1].startswith("invalid output unit")


def test_stop_control_clears_queue_and_reports_interrupted() -> None:
    async def scenario() -> PresenterApp:
        app = _app(engine=FakeEngine(chunk_seconds=0.05, chunks=4, delay=0.05))
        for i in range(4):
            await app.on_unit({"speech": f"句子{i}"})
        # let the round start
        deadline = asyncio.get_running_loop().time() + 1.0
        while not app.status()["playing"]:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("round never started")
            await asyncio.sleep(0.01)
        status, detail = await app.on_unit({"control": "stop"})
        assert status == "ok" and "stopped" in detail
        await asyncio.sleep(0.2)
        return app

    app = asyncio.run(scenario())
    client: RecordingClient = app._client  # type: ignore[assignment]
    engine: FakeEngine = app._engine  # type: ignore[assignment]
    queue_finished = [i for i in client.inputs if i["metadata"].get("event") == "queue_finished"]
    assert len(queue_finished) == 1, "interrupt must not double-report queue_finished"
    assert queue_finished[0]["metadata"]["interrupted"] is True
    assert app.status()["queue_depth"] == 0
    assert len(engine.calls) < 4, "prefetch/queued synthesis must be cancelled"


def test_ppt_units_show_slides_and_report_state(tmp_path: object) -> None:
    from pathlib import Path

    async def scenario() -> tuple[PresenterApp, RecordingBridge]:
        root = write_deck(Path(tmp_path), "demo", pages=2)
        bridge = RecordingBridge()
        app = _app(bridge=bridge, decks_dir=root)
        assert await app.on_unit(
            {"ppt": {"action": "open_deck", "deck": "demo"}, "speech": "这是第一页。"}
        ) == ("ok", "queued")
        await app.on_unit({"ppt": {"action": "next"}, "speech": "这是第二页。"})
        await _settled(app)
        return app, bridge

    app, bridge = asyncio.run(scenario())
    slides = [c for c in bridge.commands if c.get("type") == "ppt"]
    assert [(s["action"], s.get("page")) for s in slides] == [("show", 1), ("show", 2)]
    assert slides[0]["image"].endswith("001.png")
    status = app.status()
    assert (status["deck"], status["page"], status["total_pages"]) == ("demo", 2, 2)
    client: RecordingClient = app._client  # type: ignore[assignment]
    finished = client.inputs[0]["metadata"]
    assert finished["deck"] == "demo" and finished["page"] == 2
    assert "第 2/2 页" in client.inputs[0]["content"]


def test_queue_finished_and_status_carry_page_script(tmp_path: object) -> None:
    """ADR-0006：无视觉 Agent 从 queue_finished 与 status 拿到当前页讲稿。"""
    from pathlib import Path

    async def scenario() -> PresenterApp:
        root = write_deck(
            Path(tmp_path), "talk", pages=2, scripts=["第一页：开场与目录。", "乙" * 2000]
        )
        app = _app(decks_dir=root)
        await app.on_unit(
            {"ppt": {"action": "open_deck", "deck": "talk"}, "speech": "开场。"}
        )
        await _settled(app)
        return app

    app = asyncio.run(scenario())
    client: RecordingClient = app._client  # type: ignore[assignment]
    content = client.inputs[0]["content"]
    assert "当前幻灯片 talk 第 1/2 页" in content
    assert "本页讲稿：第一页：开场与目录。" in content
    status = app.status()
    assert status["page_script"] == "第一页：开场与目录。"

    async def long_script() -> PresenterApp:
        app = _app(decks_dir=Path(tmp_path))
        await app.on_unit({"ppt": {"action": "open_deck", "deck": "talk"}})
        await _settled(app)
        await app.on_unit({"ppt": {"action": "next"}})
        await _settled(app)
        return app

    app = asyncio.run(long_script())
    client = app._client  # type: ignore[assignment]
    content = client.inputs[-1]["content"]
    assert "本页讲稿：" in content
    assert "…（讲稿截断）" in content, "over-budget scripts are truncated"
    assert len(content) < 2000 + 200
    assert app.status()["page_script"].endswith("…（讲稿截断）")


def test_ppt_failure_is_async_error_and_speech_continues(tmp_path: object) -> None:
    from pathlib import Path

    async def scenario() -> tuple[PresenterApp, RecordingBridge]:
        bridge = RecordingBridge()
        app = _app(bridge=bridge, decks_dir=Path(tmp_path))  # empty decks root
        await app.on_unit({"ppt": {"action": "next"}})
        await app.on_unit({"speech": "照样说话。"})
        await _settled(app)
        return app, bridge

    app, bridge = asyncio.run(scenario())
    client: RecordingClient = app._client  # type: ignore[assignment]
    engine: FakeEngine = app._engine  # type: ignore[assignment]
    assert any(
        status == "error" and detail.startswith("ppt next failed")
        for status, detail in client.results
    )
    assert engine.calls == ["照样说话。"]
    assert client.inputs[0]["metadata"]["event"] == "queue_finished"
    assert not any(c.get("type") == "ppt" for c in bridge.commands)


class _GateEngine:
    """Fake engine gating the first chunk of selected calls behind events.

    Call ``index`` yields nothing until its gate opens, modelling the real
    multi-second TTS first-chunk latency (measured 3-8 s on audio.cpp).
    """

    name = "gate"

    def __init__(self, gates: dict[int, asyncio.Event]) -> None:
        self.calls: list[str] = []
        self._gates = gates

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        index = len(self.calls)
        self.calls.append(text)
        gate = self._gates.get(index)
        if gate is not None:
            await gate.wait()
        for _ in range(2):
            yield b"\x00\x00" * 1600  # 0.05 s of 16 kHz mono s16le per chunk


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> bool:
    """Poll ``predicate`` on a tick; return whether it held before timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def test_lookahead_starts_next_synthesis_during_first_chunk_wait() -> None:
    """合成空窗回归：下一句的预合成必须在当前句等待首块期间被捡起。

    hub 比首句开播晚 ~10ms 才派发下一 unit；lookahead 只取一次会永久错过，
    下一句直到当前句播完才开始整段合成（实测 6~8s 静音空窗）。
    """

    async def scenario() -> tuple[list[str], bool]:
        first_chunk = asyncio.Event()
        engine = _GateEngine({0: first_chunk})
        app = _app(engine=engine)
        assert await app.on_unit({"speech": "第一句"}) == ("ok", "queued")
        assert await _wait_until(lambda: engine.calls == ["第一句"])
        await app.on_unit({"speech": "第二句"})
        # 第一句仍无音频：第二句必须被预合成捡起（旧实现此时永久错过）。
        started_early = await _wait_until(lambda: len(engine.calls) == 2, timeout=0.5)
        first_chunk.set()
        await _settled(app)
        return engine.calls, started_early

    calls, started_early = asyncio.run(scenario())
    assert calls == ["第一句", "第二句"]
    assert started_early, "第二句未在第一句等待首块期间开始合成（预合成竞态）"


def test_subtitle_switches_only_when_first_audio_chunk_plays() -> None:
    """字幕时序回归：首块音频前不得切换字幕/开口型，屏上保留上一句。

    合成等待期（首块 3~8s）里 _speak 开头就发 subtitle_set(text, 0) 等同
    清空屏幕；音频没就绪前字幕必须停在上一句全文。
    """

    def subtitles(bridge: RecordingBridge) -> list[dict]:
        return [c for c in bridge.commands if c["type"] == "subtitle_set"]

    async def scenario() -> tuple[list[dict], list[dict], list[dict]]:
        gate1, gate2 = asyncio.Event(), asyncio.Event()
        engine = _GateEngine({0: gate1, 1: gate2})
        bridge = RecordingBridge()
        app = _app(engine=engine, bridge=bridge)
        await app.on_unit({"speech": "第一句"})
        assert await _wait_until(lambda: engine.calls == ["第一句"])
        # 首块未到：不得有任何 subtitle_set，不得报说话态。
        early_sets = list(subtitles(bridge))
        speaking = [c for c in bridge.commands if c["type"] == "speech_state" and c["active"]]
        assert early_sets == [], "首块到达前不得切换字幕"
        assert speaking == [], "首块到达前不得进入说话态"
        await app.on_unit({"speech": "第二句"})
        gate1.set()
        assert await _wait_until(lambda: app.status()["completed_sentences"] == 1)
        # 句 1 播完、句 2 仍在合成（gate2 未开）：字幕必须仍是句 1 全文。
        assert await _wait_until(lambda: len(engine.calls) == 2)
        await asyncio.sleep(0.2)  # 给 worker 进入句 2 的 _speak 的时间
        held = list(subtitles(bridge))
        gate2.set()
        await _settled(app)
        return early_sets, held, subtitles(bridge)

    early_sets, held, final_sets = asyncio.run(scenario())
    assert early_sets == [] and held, "前置断言应在 scenario 内失败"
    last = held[-1]
    assert last["text"] == "第一句", f"合成等待期字幕被提前切换: {last}"
    assert last["visible_chars"] == len("第一句")
    assert final_sets[-1]["text"] == "第二句"
    assert final_sets[-1]["visible_chars"] == len("第二句")
