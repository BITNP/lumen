"""Entry point for the presenter host process (the waycast module identity)."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from pathlib import Path

from dotenv import load_dotenv

from presenter.app import PresenterApp, build_engine
from presenter.config import load_settings
from presenter.playback import AudioPlayer
from presenter.presentation import GodotSupervisor, PresentationBridge

LOGGER = logging.getLogger("presenter")


async def _run() -> None:
    settings = load_settings()
    engine = build_engine(settings)
    player = AudioPlayer(device=settings.audio_device)
    player.start()
    bridge = PresentationBridge(
        port=settings.bridge_port,
    )
    supervisor = (
        GodotSupervisor(settings.godot_path, str(Path(__file__).resolve().parents[2] / "godot"))
        if settings.godot_path
        else None
    )
    app = PresenterApp(settings, engine, player, bridge, supervisor)
    bridge.set_user_event_handler(app.on_user_event)
    await bridge.start()
    loop = asyncio.get_running_loop()
    stop = loop.create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, lambda: stop.done() or stop.set_result(None))
    runner = asyncio.create_task(app.run_forever())
    try:
        await stop
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner
        if supervisor is not None:
            await supervisor.stop()
        await bridge.stop()
        player.stop()


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("PRESENTER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
