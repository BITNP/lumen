"""Full-duplex hub client contract for a kind="both" module (ADR-0008).

The test server stands in for the waycast hub: capability handshake with an
output schema, pushed output_unit answered by a result frame, status_query
answered by a correlated status_result, and outbound input frames.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
import websockets

from presenter.hub_client import HubConnectionError, PresenterClient
from presenter.schema import MODULE_NAME, OUTPUT_SCHEMA
from presenter.wire import dumps, make_frame, parse_frame


async def _never_unit(payload: dict[str, Any]) -> tuple[str, str]:  # pragma: no cover
    return "ok", ""


def _fake_hub(received: list[Any]) -> Any:
    async def handler(ws: Any) -> None:
        cap = parse_frame(await ws.recv())
        received.append(("capability", cap))
        await ws.send(dumps(make_frame("capability", MODULE_NAME, {"accepted": True})))
        await ws.send(dumps(make_frame("output_unit", MODULE_NAME, {"speech": "你好"})))
        received.append(("await_result", parse_frame(await ws.recv())))
        await ws.send(dumps(make_frame("status_query", MODULE_NAME, {}, request_id="q1")))
        received.append(("status", parse_frame(await ws.recv())))
        received.append(("input", parse_frame(await ws.recv())))

    return websockets.serve(handler, "127.0.0.1", 0)


def test_both_handshake_and_frame_routing() -> None:
    received: list[Any] = []

    async def on_unit(payload: dict[str, Any]) -> tuple[str, str]:
        assert payload == {"speech": "你好"}
        return "ok", "queued"

    async def scenario() -> None:
        server = await _fake_hub(received)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with PresenterClient(
            hub_url=url,
            token="t",
            on_unit=on_unit,
            status_provider=lambda: {"playing": False, "tts_engine": "fake"},
        ) as client:
            await asyncio.sleep(0.2)  # let the reader route unit + status frames
            await client.send_input("前端点击", metadata={"event": "user_interaction"})
            await asyncio.sleep(0.05)
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
    kinds = [name for name, _ in received]
    assert kinds == ["capability", "await_result", "status", "input"]

    cap = received[0][1]
    assert cap["type"] == "capability"
    assert cap["payload"]["kind"] == "both"
    assert cap["payload"]["input"]["description"]
    assert cap["payload"]["output"]["schema"] == OUTPUT_SCHEMA
    assert cap["payload"]["status"]["enabled"] is True

    result = received[1][1]
    assert result["type"] == "result"
    assert result["payload"] == {"module": MODULE_NAME, "status": "ok", "detail": "queued"}

    status = received[2][1]
    assert status["type"] == "status_result"
    assert status["request_id"] == "q1"
    assert status["payload"]["playing"] is False

    inp = received[3][1]
    assert inp["type"] == "input"
    assert inp["payload"]["metadata"]["event"] == "user_interaction"


def test_rejected_capability_raises() -> None:
    async def handler(ws: Any) -> None:
        await ws.recv()
        await ws.send(dumps(make_frame("capability", MODULE_NAME, {"accepted": False})))

    async def scenario() -> None:
        server = await websockets.serve(handler, "127.0.0.1", 0)
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            with pytest.raises(HubConnectionError):
                async with PresenterClient(
                    hub_url=url,
                    token="t",
                    on_unit=_never_unit,
                    status_provider=lambda: {},
                ):
                    pass
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(scenario())
