"""Entry point for the presenter host process (the waycast module identity)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

from dotenv import load_dotenv

from presenter.app import PresenterApp, build_engine
from presenter.config import load_settings
from presenter.playback import AudioPlayer
from presenter.presentation import GodotSupervisor, PresentationBridge

LOGGER = logging.getLogger("presenter")

LOG_LEVEL_KEY = "PRESENTER_LOG_LEVEL"
DEPS_LOG_LEVEL_KEY = "PRESENTER_DEPS_LOG_LEVEL"
# 第三方噪音源（WS 帧、HTTP 请求、事件循环）默认钉在 WARNING，本模块的
# TTS/播放/队列调试日志（DEBUG）不受影响；PRESENTER_DEPS_LOG_LEVEL 可放开。
# httpx2/httpcore2 是 openai SDK 内置 vendored 客户端的日志器名。
_THIRD_PARTY_LOGGERS = (
    "websockets",
    "websockets.client",
    "websockets.server",
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore2",
    "asyncio",
    "urllib3",
)


def configure_logging() -> None:
    """本模块日志级别走 PRESENTER_LOG_LEVEL（默认 INFO），依赖库走
    PRESENTER_DEPS_LOG_LEVEL（默认 WARNING）。"""
    level = os.environ.get(LOG_LEVEL_KEY, "INFO").strip().upper() or "INFO"
    deps_level = (
        os.environ.get(DEPS_LOG_LEVEL_KEY, "WARNING").strip().upper() or "WARNING"
    )
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for name in _THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(deps_level)


async def _run() -> None:
    settings = load_settings()
    engine = build_engine(settings)
    player = AudioPlayer(device=settings.audio_device)
    player.start()
    bridge = PresentationBridge(
        port=settings.bridge_port,
    )
    supervisor = (
        GodotSupervisor(
            settings.godot_path, str(Path(__file__).resolve().parents[2] / "godot")
        )
        if settings.godot_path
        else None
    )
    app = PresenterApp(settings, engine, player, bridge, supervisor)
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
        await app.shutdown()
        if supervisor is not None:
            await supervisor.stop()
        await bridge.stop()
        player.stop()
        close = getattr(engine, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await cast("Callable[[], Awaitable[None]]", close)()


def main() -> None:
    load_dotenv()
    configure_logging()
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
