"""Playback-queue pipeline: order, accept results, stop, takeover, sync (ADR-0008).

Uses tests/placeholders (silent/paced player, fake engine, recording client);
the real synthesis path is covered by the real-marked smoke test.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable

from placeholders import (
    FakeEngine,
    PacedPlayer,
    RecordingBridge,
    RecordingClient,
    ScriptedEngine,
    SilentPlayer,
    write_deck,
)
from presenter.app import PresenterApp
from presenter.config import load_settings
from presenter.playback import PlayerSink
from presenter.presentation import PresentationBridge
from presenter.tts import TtsEngine


def _app(
    *,
    engine: TtsEngine | None = None,
    queue_max: int = 64,
    bridge: PresentationBridge | None = None,
    player: PlayerSink | None = None,
    decks_dir: object = None,
    sentence_gap: float = 0.0,
) -> PresenterApp:
    env = {
        "WAYCAST_HUB_URL": "ws://127.0.0.1:1",
        "PRESENTER_QUEUE_MAX": str(queue_max),
        "PRESENTER_SENTENCE_GAP_SECONDS": str(sentence_gap),
    }
    if decks_dir is not None:
        env["PRESENTER_DECKS_DIR"] = str(decks_dir)
    settings = load_settings(env)
    app = PresenterApp(
        settings,
        engine or FakeEngine(),
        player or SilentPlayer(),
        bridge or PresentationBridge(port=0),
    )
    app._client = RecordingClient()  # type: ignore[assignment]
    return app


async def _settled(app: PresenterApp, *, seconds: float = 3.0) -> None:
    """Wait until the playback queue drained and its queue_finished settled."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if app._playback.settled:
            await asyncio.sleep(0.05)  # let the queue_finished send settle
            return
        await asyncio.sleep(0.02)
    raise AssertionError("playback queue did not drain")


def _finished_events(client: RecordingClient) -> list[dict]:
    return [i for i in client.inputs if i["metadata"].get("event") == "queue_finished"]


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 1.0) -> bool:
    """Poll ``predicate`` on a tick; return whether it held before timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            return False
        await asyncio.sleep(0.01)
    return True


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
    finished = _finished_events(client)
    assert len(finished) == 1
    assert finished[0]["metadata"]["event"] == "queue_finished"
    assert finished[0]["metadata"]["interrupted"] is False
    assert finished[0]["metadata"]["completed_sentences"] == 2


def test_subtitle_stays_after_round_ends() -> None:
    """ADR-0010 表现语义：轮次结束不清空字幕，直到下一条字幕开始替换。"""

    async def scenario() -> RecordingBridge:
        bridge = RecordingBridge()
        app = _app(bridge=bridge)
        assert await app.on_unit({"speech": "播完保留的字幕"}) == ("ok", "queued")
        await _settled(app)
        return bridge

    bridge = asyncio.run(scenario())
    subtitles = [
        c for c in bridge.commands if c["type"] in ("subtitle_set", "subtitle_clear")
    ]
    assert subtitles, "sentence playback should drive subtitle messages"
    assert all(c["type"] == "subtitle_set" for c in subtitles), (
        "round end must not clear the subtitle"
    )
    final = subtitles[-1]
    assert final["text"] == "播完保留的字幕"
    assert final["visible_chars"] == len(final["text"])


def test_visual_only_units_finish_without_synthesis() -> None:
    async def scenario() -> RecordingClient:
        app = _app()
        await app.on_unit({"emotion": "wink"})
        await app.on_unit({"motion": "wave"})
        await _settled(app)
        return app._client  # type: ignore[return-value]

    client = asyncio.run(scenario())
    assert _finished_events(client)[0]["metadata"]["completed_sentences"] == 2


def test_queue_overflow_returns_error_result() -> None:
    async def scenario() -> tuple[PresenterApp, list[tuple[str, str]]]:
        app = _app(queue_max=2)
        results = [await app.on_unit({"emotion": "wink"}) for _ in range(3)]
        await _settled(app)
        return app, results

    app, results = asyncio.run(scenario())
    assert results[0][0] == results[1][0] == "ok"
    assert results[2] == ("error", "playback queue full")
    client: RecordingClient = app._client  # type: ignore[assignment]
    assert _finished_events(client)[0]["metadata"]["completed_sentences"] == 2


def test_invalid_unit_is_rejected_without_queueing() -> None:
    async def scenario() -> tuple[tuple[str, str], tuple[str, str]]:
        app = _app()
        bad = await app.on_unit({"control": "replace"})
        empty = await app.on_unit({"speech": "  "})
        assert not app.status()["playing"], "rejected units must not start a round"
        return bad, empty

    bad, empty = asyncio.run(scenario())
    assert bad[0] == "error" and "control" in bad[1]
    assert empty[0] == "error" and empty[1].startswith("invalid output unit")


def test_stop_drops_backlog_keeps_playing_head_and_reports() -> None:
    """ADR-0008 stop：正在播的队头不被掐、继续播完；其后的任务全部丢弃；
    interrupted 心跳立即发出，幸存队头播完后排空心跳照常发出（两者独立）。"""

    async def scenario() -> tuple[PresenterApp, SilentPlayer]:
        player = SilentPlayer()
        app = _app(
            engine=FakeEngine(chunk_seconds=0.05, chunks=4, delay=0.05), player=player
        )
        for i in range(4):
            await app.on_unit({"speech": f"句子{i}"})
        assert await _wait_until(lambda: bool(player.enqueued), timeout=2.0), (
            "队头从未开播"
        )
        status, detail = await app.on_unit({"control": "stop"})
        assert status == "ok" and "dropped 3" in detail
        await asyncio.sleep(0.5)  # 幸存队头把余下音频播完并触发排空心跳
        return app, player

    app, player = asyncio.run(scenario())
    client: RecordingClient = app._client  # type: ignore[assignment]
    engine: FakeEngine = app._engine  # type: ignore[assignment]
    finished = _finished_events(client)
    assert [f["metadata"]["interrupted"] for f in finished] == [True, False]
    assert finished[0]["metadata"]["completed_sentences"] == 0
    assert finished[1]["metadata"]["completed_sentences"] == 1
    assert app.status()["queue_depth"] == 0
    assert player.cleared == 0, "stop 绝不允许掐正在播放的队头"
    assert len(engine.calls) < 4, "被丢弃的预合成必须停止，不再整句合成"


def test_takeover_cuts_playing_head_when_new_audio_is_ready() -> None:
    """ADR-0008 无缝接管：stop 保留当前队头，但 stop 后的新任务首个音频字节
    就绪的刹那立刻掐掉队头开播（实测掐断点必须落在队头音频放完之前）。"""

    async def scenario() -> tuple[RecordingBridge, RecordingClient, PacedPlayer, float]:
        player = PacedPlayer()
        bridge = RecordingBridge()
        # 每句合成 ~0.3s、音频 1.0s：s1 有余量在被掐前一直播。
        engine = FakeEngine(chunk_seconds=0.1, chunks=10, delay=0.03)
        app = _app(engine=engine, bridge=bridge, player=player)
        await app.on_unit({"speech": "第一句"})
        assert await _wait_until(lambda: bool(player.enqueued), timeout=2.0), (
            "s1 未开播"
        )
        assert await app.on_unit({"control": "stop"}) == (
            "ok",
            "stopped, dropped 0 queued unit(s)",
        )
        await app.on_unit({"speech": "第二句"})
        opened = time.monotonic()
        assert await _wait_until(
            lambda: any(
                c["type"] == "subtitle_set" and c["text"] == "第二句"
                for c in bridge.commands
            ),
            timeout=2.0,
        ), "接管没有发生"
        cut_at = player.written_seconds  # s2 开播瞬间：s1 的 1.0s 音频远未放完
        await _settled(app, seconds=5.0)
        assert time.monotonic() - opened < 2.0
        return bridge, app._client, player, cut_at  # type: ignore[return-value]

    bridge, client, player, cut_at = asyncio.run(scenario())
    assert player.cleared == 1, "接管必须精确地掐一次播放器"
    assert cut_at < 0.9, (
        f"接管发生在队头音频播完之后（{cut_at:.2f}s），成了续播不是接管"
    )
    finished = _finished_events(client)
    assert [f["metadata"]["interrupted"] for f in finished] == [False]
    assert finished[0]["metadata"]["completed_sentences"] == 2


def test_actions_fire_at_playback_start_not_before_synthesis() -> None:
    """动作/PPT/字幕与开播同步：合成等待期（首块前）不得向呈现进程发任何
    emotion/motion/subtitle/speech_state；首块交给播放器后同批到达。"""

    async def scenario() -> tuple[RecordingBridge, list[str]]:
        gate = asyncio.Event()
        engine = _GateEngine({0: gate})
        bridge = RecordingBridge()
        player = SilentPlayer()
        app = _app(engine=engine, bridge=bridge, player=player)
        await app.on_unit(
            {"speech": "带动作的一句", "emotion": "wink", "motion": "wave"}
        )
        assert await _wait_until(lambda: engine.calls == ["带动作的一句"])
        await asyncio.sleep(0.2)  # 合成等待期早已越过任何 tick
        during_synth = [c["type"] for c in bridge.commands]
        gate.set()
        assert await _wait_until(
            lambda: any(c["type"] == "subtitle_set" for c in bridge.commands),
            timeout=2.0,
        )
        return bridge, during_synth

    bridge, during_synth = asyncio.run(scenario())
    assert during_synth == [], f"合成等待期就向屏幕发了动作: {during_synth}"
    types = [c["type"] for c in bridge.commands]
    assert {"emotion", "motion", "subtitle_set", "speech_state"} <= set(types)
    assert types.index("emotion") < types.index("subtitle_set")
    first_subtitle = types.index("subtitle_set")
    assert all(t in ("emotion", "motion") for t in types[:first_subtitle])


def test_prefetch_synthesizes_far_ahead_of_playback() -> None:
    """ADR-0008 预合成：后台串行合成沿队列一路向前——第三句在第一句还在播
    时早已合成完毕（旧实现 lookahead 深度 1 做不到）。"""

    async def scenario() -> tuple[list[str], bool]:
        player = PacedPlayer()
        engine = FakeEngine(
            chunk_seconds=0.5, chunks=1, delay=0.1
        )  # 合成 0.1s/句，音频 0.5s/句
        app = _app(engine=engine, player=player)
        for i in range(3):
            await app.on_unit({"speech": f"句子{i}"})
        ahead = await _wait_until(
            lambda: (
                len(engine.calls) == 3
                and bool(player.enqueued)
                and player.written_seconds < 0.4
            ),
            timeout=1.0,
        )
        await _settled(app)
        return engine.calls, ahead

    calls, ahead = asyncio.run(scenario())
    assert calls == ["句子0", "句子1", "句子2"]
    assert ahead, "预合成没有跑在播放前面（合成完 3 句时首句音频已放了 0.4s 以上）"


def test_synth_failure_reports_error_and_queue_continues() -> None:
    """某句合成失败：error result + 跳过该句，后续句子照常播、排空心跳照发。"""

    async def scenario() -> RecordingClient:
        app = _app(engine=ScriptedEngine("fake", ["fail", "ok"]))
        await app.on_unit({"speech": "会失败的一句"})
        await app.on_unit({"speech": "照常播的一句"})
        await _settled(app)
        return app._client  # type: ignore[return-value]

    client = asyncio.run(scenario())
    assert any(s == "error" and "synthesis failed" in d for s, d in client.results)
    finished = _finished_events(client)
    assert finished[0]["metadata"]["completed_sentences"] == 1


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
    finished = _finished_events(client)[0]["metadata"]
    assert finished["deck"] == "demo" and finished["page"] == 2
    assert "第 2/2 页" in client.inputs[0]["content"]


def test_queue_finished_and_status_carry_page_texts(tmp_path: object) -> None:
    """ADR-0006/0007：无视觉 Agent 从 queue_finished 与 status 拿到页面内容与讲稿。"""
    from pathlib import Path

    async def scenario() -> PresenterApp:
        root = write_deck(
            Path(tmp_path),
            "talk",
            pages=2,
            summaries=["第一页：开场与目录。", "乙" * 2000],
            scripts=["轻松开场，念一下标题。", ""],
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
    assert "本页内容：第一页：开场与目录。" in content
    assert "建议讲稿：轻松开场，念一下标题。" in content
    status = app.status()
    assert status["page_summary"] == "第一页：开场与目录。"
    assert status["page_script"] == "轻松开场，念一下标题。"

    async def long_text() -> PresenterApp:
        app = _app(decks_dir=Path(tmp_path))
        await app.on_unit({"ppt": {"action": "open_deck", "deck": "talk"}})
        await _settled(app)
        await app.on_unit({"ppt": {"action": "next"}})
        await _settled(app)
        return app

    app = asyncio.run(long_text())
    client = app._client  # type: ignore[assignment]
    content = client.inputs[-1]["content"]
    assert "本页内容：" in content
    assert "…（已截断）" in content, "over-budget page texts are truncated"
    assert "建议讲稿：" not in content, "blank script is omitted"
    assert len(content) < 2000 + 200
    assert app.status()["page_summary"].endswith("…（已截断）")
    assert app.status()["page_script"] is None


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
    assert _finished_events(client)[0]["metadata"]["event"] == "queue_finished"
    assert not any(c.get("type") == "ppt" for c in bridge.commands)


def test_sentence_gap_holds_previous_subtitle_between_sentences() -> None:
    """句间停顿（PRESENTER_SENTENCE_GAP_SECONDS）：上一句播完后屏上停一拍再切下一句。"""

    async def scenario(gap: float) -> tuple[RecordingBridge, float]:
        bridge = RecordingBridge()
        app = _app(bridge=bridge, sentence_gap=gap)
        await app.on_unit({"speech": "第一句"})
        await app.on_unit({"speech": "第二句"})
        started = time.monotonic()
        await _settled(app, seconds=5.0)
        return bridge, time.monotonic() - started

    bridge, _ = asyncio.run(scenario(0.6))
    stamps = list(zip(bridge.commands, bridge.arrived_at))
    first_full = max(
        t
        for command, t in stamps
        if command["type"] == "subtitle_set"
        and command["text"] == "第一句"
        and command["visible_chars"] == len("第一句")
    )
    second_start = min(
        t
        for command, t in stamps
        if command["type"] == "subtitle_set" and command["text"] == "第二句"
    )
    assert second_start - first_full >= 0.55, "句间停顿不足，上一句字幕没停够一拍"
    _, tight_seconds = asyncio.run(scenario(0.0))
    assert tight_seconds < 1.5, "gap=0 时不应引入停顿"


def test_first_sentence_never_waits_for_the_gap() -> None:
    """首句没有句间停顿：gap 只发生在句与句之间，开场立即可讲。"""

    async def scenario() -> bool:
        bridge = RecordingBridge()
        app = _app(bridge=bridge, sentence_gap=1.0)
        await app.on_unit({"speech": "第一句"})
        return await _wait_until(
            lambda: any(c["type"] == "subtitle_set" for c in bridge.commands),
            timeout=0.5,
        )

    assert asyncio.run(scenario()), "首句被句间停顿延迟了"


def test_gap_does_not_stack_on_slow_synthesis() -> None:
    """gap 是句间最少间隔而非额外延迟：合成等待本身超过 gap 时不再叠加等待。

    第二句的首块在上一句播完 1s 后才到达（gap 0.5s）：字幕应随首块立即
    切换，而不是再补睡 0.5s（1.5s 才开口）。
    """

    async def scenario() -> tuple[bool, float]:
        gate1, gate2 = asyncio.Event(), asyncio.Event()
        engine = _GateEngine({0: gate1, 1: gate2})
        bridge = RecordingBridge()
        app = _app(engine=engine, bridge=bridge, sentence_gap=0.5)
        client: RecordingClient = app._client  # type: ignore[assignment]
        await app.on_unit({"speech": "第一句"})
        assert await _wait_until(lambda: engine.calls == ["第一句"])
        await app.on_unit({"speech": "第二句"})
        gate1.set()
        assert await _wait_until(lambda: app.status()["completed_sentences"] == 1)
        assert await _wait_until(lambda: len(engine.calls) == 2)
        await asyncio.sleep(1.0)  # 合成等待已超过 gap，停顿应完全被其吸收
        opened = time.monotonic()
        gate2.set()
        appeared = await _wait_until(
            lambda: any(
                c["type"] == "subtitle_set" and c["text"] == "第二句"
                for c in bridge.commands
            ),
            timeout=2.0,
        )
        elapsed = time.monotonic() - opened
        # 轮次在字幕出现后瞬间收尾（末句无停顿），等 queue_finished 落定即可。
        assert await _wait_until(
            lambda: any(
                i["metadata"].get("event") == "queue_finished" for i in client.inputs
            ),
            timeout=2.0,
        )
        return appeared, elapsed

    appeared, after_gate = asyncio.run(scenario())
    assert appeared
    assert after_gate < 0.3, (
        f"停顿叠加在合成等待之上（开闸后 {after_gate:.2f}s 才开口）"
    )


def test_stop_interrupts_the_sentence_gap() -> None:
    """停顿期间 stop：未开播的下一句被丢弃、不开口，interrupted 心跳照发。"""

    async def scenario() -> tuple[RecordingBridge, RecordingClient]:
        bridge = RecordingBridge()
        app = _app(bridge=bridge, sentence_gap=2.0)
        client: RecordingClient = app._client  # type: ignore[assignment]
        await app.on_unit({"speech": "第一句"})
        await app.on_unit({"speech": "第二句"})
        held = await _wait_until(
            lambda: any(
                c["type"] == "subtitle_set"
                and c["text"] == "第一句"
                and c["visible_chars"] == len("第一句")
                for c in bridge.commands
            ),
            timeout=2.0,
        )
        assert held, "第一句没有播完"
        await asyncio.sleep(0.1)  # 此刻应正处于句间停顿的等待中
        await app.on_unit({"control": "stop"})
        await asyncio.sleep(0.3)
        return bridge, client

    bridge, client = asyncio.run(scenario())
    assert not any(
        c["type"] == "subtitle_set" and c["text"] == "第二句" for c in bridge.commands
    ), "stop 后停顿结束仍开口了第二句"
    finished = _finished_events(client)
    assert len(finished) == 1 and finished[0]["metadata"]["interrupted"] is True


def test_no_gap_pause_after_last_sentence() -> None:
    """末句之后没有下一句：不等待句间停顿，直接发 queue_finished。"""

    async def scenario() -> float:
        app = _app(sentence_gap=0.8)
        started = time.monotonic()
        await app.on_unit({"speech": "只此一句"})
        await _settled(app, seconds=3.0)
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())
    assert elapsed < 0.8, f"末句后不应等待句间停顿（实测 {elapsed:.2f}s）"


class _GateEngine:
    """Fake engine gating the first chunk of selected calls behind events.

    Call ``index`` yields nothing until its gate opens, modelling the real
    multi-second TTS first-chunk latency (measured 3-8 s on audio.cpp).
    """

    name = "gate"

    def __init__(self, gates: dict[int, asyncio.Event]) -> None:
        self.calls: list[str] = []
        self._gates = gates

    async def stream(self, text: str) -> AsyncGenerator[bytes, None]:
        index = len(self.calls)
        self.calls.append(text)
        gate = self._gates.get(index)
        if gate is not None:
            await gate.wait()
        for _ in range(2):
            yield b"\x00\x00" * 1600  # 0.05 s of 16 kHz mono s16le per chunk


def test_subtitle_switches_only_when_first_audio_chunk_plays() -> None:
    """字幕时序回归：首块音频前不得切换字幕/开口型，屏上保留上一句。

    合成等待期（首块 3~8s）里就发 subtitle_set(text, 0) 等同清空屏幕；
    音频没就绪前字幕必须停在上一句全文。
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
        speaking = [
            c for c in bridge.commands if c["type"] == "speech_state" and c["active"]
        ]
        assert early_sets == [], "首块到达前不得切换字幕"
        assert speaking == [], "首块到达前不得进入说话态"
        await app.on_unit({"speech": "第二句"})
        gate1.set()
        assert await _wait_until(lambda: app.status()["completed_sentences"] == 1)
        # 句 1 播完、句 2 仍在合成（gate2 未开）：字幕必须仍是句 1 全文。
        assert await _wait_until(lambda: len(engine.calls) == 2)
        await asyncio.sleep(0.2)  # 给播放循环进入句 2 首块等待的时间
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
