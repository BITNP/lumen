"""Expression pipeline: order, accept results, queue overflow, stop control.

Uses tests/placeholders (silent player, fake engine, recording client); the
real synthesis path is covered by the real-marked smoke test.
"""
from __future__ import annotations

import asyncio

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


def _app(
    *,
    engine: FakeEngine | None = None,
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
