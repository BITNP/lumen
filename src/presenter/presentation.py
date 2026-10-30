"""Presentation bridge: the host-side of the internal host <-> Godot channel.

The bridge is a localhost WebSocket server (ADR-0002); its message shapes are
module-internal and NOT part of the waycast wire protocol.

Bridge protocol (host -> presentation process), one JSON object per frame:
  {"type": "subtitle_set", "text": str, "visible_chars": int}
  {"type": "subtitle_clear"}
  {"type": "emotion", "tag": str} / {"type": "motion", "tag": str}
  {"type": "ppt", ...}                      (M3)
Presentation process -> host:
  {"type": "hello"}
  {"type": "user_event", "kind": str, "detail": str}   -> forwarded as input

If PRESENTER_GODOT_PATH is configured the bridge also supervises the Godot
process: exit or crash restarts it with exponential backoff; while no client
is attached, presentation commands are dropped and status reports
presentation_available=false (the host keeps speaking regardless).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any, cast

import websockets
from websockets.asyncio.server import ServerConnection

LOGGER = logging.getLogger(__name__)

RestartGodot = Callable[[], "asyncio.Task[None] | None"]


class PresentationBridge:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 8790,
        on_user_event: Callable[[str, str], Any] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._on_user_event = on_user_event
        self._clients: set[ServerConnection] = set()
        self._server: Any = None

    @property
    def available(self) -> bool:
        return bool(self._clients)

    def set_user_event_handler(self, handler: Callable[[str, str], Any]) -> None:
        """Wire the host-side sink for user events (set by PresenterApp)."""
        self._on_user_event = handler

    async def start(self) -> None:
        self._server = await websockets.serve(
            self._handle, self._host, self._port
        )
        LOGGER.info("presentation bridge listening on ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._clients.clear()

    async def send(self, command: dict[str, Any]) -> None:
        """Fire-and-forget a presentation command; dropped when no window is up."""
        if not self._clients:
            LOGGER.debug("presentation unavailable, dropping %s", command.get("type"))
            return
        raw = json.dumps(command, ensure_ascii=False)
        for client in list(self._clients):
            try:
                await client.send(raw)
            except Exception:
                self._clients.discard(client)

    async def _handle(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
        LOGGER.info("presentation process connected")
        try:
            async for raw in ws:
                try:
                    parsed: object = json.loads(raw)
                except (json.JSONDecodeError, UnicodeError):
                    continue
                if not isinstance(parsed, dict):
                    continue
                frame = cast("dict[str, Any]", parsed)
                if frame.get("type") == "user_event" and self._on_user_event is not None:
                    kind = str(frame.get("kind", "unknown"))
                    detail = str(frame.get("detail", ""))
                    result = self._on_user_event(kind, detail)
                    if asyncio.iscoroutine(result):
                        await result
        except Exception:
            pass
        finally:
            self._clients.discard(ws)
            LOGGER.warning("presentation process disconnected")


class GodotSupervisor:
    """Start/restart the Godot presentation process when a binary is configured."""

    def __init__(self, godot_path: str, project_dir: str) -> None:
        self._godot_path = godot_path
        self._project_dir = project_dir
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _supervise(self) -> None:
        delay = 1.0
        while not self._stopping:
            proc: asyncio.subprocess.Process | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    self._godot_path, "--path", self._project_dir,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                LOGGER.info("presentation process started (pid=%s)", proc.pid)
                await proc.wait()
                delay = 1.0
                LOGGER.warning("presentation process exited rc=%s", proc.returncode)
            except asyncio.CancelledError:
                if proc is not None:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                raise
            except OSError as exc:
                LOGGER.error("cannot start presentation process: %s", exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


__all__ = ["PresentationBridge", "GodotSupervisor"]
