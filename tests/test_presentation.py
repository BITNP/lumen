"""Presentation bridge: availability tracking, command fan-out, and the
display-only contract (ADR-0005): frames from the presentation process are
received but never turn into anything upstream."""

from __future__ import annotations

import asyncio
import json

import websockets

from lumen.presentation import PresentationBridge


def test_bridge_drops_without_clients_and_ignores_upstream() -> None:
    async def scenario() -> tuple[list[dict], bool, bool]:
        bridge = PresentationBridge(host="127.0.0.1", port=0)
        await bridge.start()
        port = bridge._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        assert bridge.available is False
        await bridge.send(
            {"type": "subtitle_clear"}
        )  # no window: dropped, must not raise

        async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
            await client.send(json.dumps({"type": "hello"}))
            for _ in range(50):
                if bridge.available:
                    break
                await asyncio.sleep(0.02)
            connected = bridge.available
            await bridge.send({"type": "speech_state", "active": True})
            echoed = [json.loads(await client.recv())]
            # A rogue upstream frame is drained and ignored, connection lives on.
            await client.send(json.dumps({"type": "user_event", "kind": "click"}))
            await bridge.send({"type": "subtitle_clear"})
            echoed.append(json.loads(await client.recv()))
        for _ in range(50):
            if not bridge.available:
                break
            await asyncio.sleep(0.02)
        await bridge.stop()
        return echoed, connected, bridge.available

    echoed, connected, still_on = asyncio.run(scenario())
    assert connected is True
    assert [e["type"] for e in echoed] == ["speech_state", "subtitle_clear"]
    assert still_on is False, "detach must clear availability"
