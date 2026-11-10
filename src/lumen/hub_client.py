"""Waycast hub connection for a kind="both" module.

Full-duplex on one connection (ADR-0008): the lumen sends its capability
advertisement first, then input / result / status_result frames; it receives
the capability acknowledgement, output_unit frames and status_query frames.
The wire contract is mirrored locally in lumen.wire; no core imports.

Unlike voice_input's input-only client, received output_unit frames are handed
to an async on_unit callback and every handled unit is answered with a result
frame (accept semantics, ADR-0002/0003).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Self
from urllib.parse import urlencode, urlparse, urlunparse

import websockets

from lumen.schema import (
    CAPABILITY_INPUT_DESCRIPTION,
    CAPABILITY_OUTPUT_DESCRIPTION,
    CAPABILITY_STATUS_DESCRIPTION,
    MODULE_NAME,
    OUTPUT_SCHEMA,
)
from lumen.wire import dumps, make_frame, parse_frame

LOGGER = logging.getLogger(__name__)

OnUnit = Callable[[dict[str, Any]], Awaitable[tuple[str, str]]]


class HubConnectionError(RuntimeError):
    """Raised when the hub rejects the module connection."""


class LumenClient:
    """Connected lumen module: send input/result, dispatch output_unit.

    Usage:

        async with LumenClient(hub_url=..., token=..., on_unit=...) as client:
            await client.send_input(...)

    Reconnection is the caller's job (LumenApp.run_forever wraps this).
    """

    def __init__(
        self,
        *,
        hub_url: str,
        token: str,
        on_unit: OnUnit,
        status_provider: Callable[[], dict[str, Any]],
        tls_ca_path: str | None = None,
        name: str = MODULE_NAME,
    ) -> None:
        self._hub_url = hub_url
        self._token = token
        self._on_unit = on_unit
        self._status_provider = status_provider
        self._tls_ca = tls_ca_path
        self._name = name
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()

    async def wait_closed(self) -> None:
        """Resolve when the connection drops (reader loop exited)."""
        await self._closed.wait()

    @property
    def name(self) -> str:
        return self._name

    def _ws_uri(self) -> str:
        parts = urlparse(self._hub_url.rstrip("/") + "/ws/modules")
        query = urlencode({"token": self._token}) if self._token else parts.query
        return urlunparse(parts._replace(query=query))

    def _connect_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"open_timeout": 10.0}
        if self._tls_ca:
            import ssl

            kwargs["ssl"] = ssl.create_default_context(cafile=self._tls_ca)
        return kwargs

    async def __aenter__(self) -> Self:
        uri = self._ws_uri()
        try:
            self._ws = await websockets.connect(uri, **self._connect_kwargs())
        except OSError as exc:
            raise HubConnectionError(f"cannot reach hub at {uri}: {exc}") from exc
        capability = {
            "name": self._name,
            "kind": "both",
            "input": {"description": CAPABILITY_INPUT_DESCRIPTION},
            "output": {
                "schema": OUTPUT_SCHEMA,
                "description": CAPABILITY_OUTPUT_DESCRIPTION,
            },
            "status": {"enabled": True, "description": CAPABILITY_STATUS_DESCRIPTION},
        }
        try:
            await self._ws.send(dumps(make_frame("capability", self._name, capability)))
            ack = parse_frame(await self._ws.recv())
            if ack["type"] != "capability" or not ack["payload"].get("accepted"):
                raise HubConnectionError(f"hub did not accept capability: {dumps(ack)}")
        except HubConnectionError:
            await self._ws.close()
            raise
        except Exception as exc:
            await self._ws.close()
            raise HubConnectionError("hub handshake failed") from exc
        self._reader = asyncio.create_task(self._read_loop())
        LOGGER.info("%s admitted to waycast hub (kind=both)", self._name)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _read_loop(self) -> None:
        """Route output_unit and status_query frames; surface remote closure."""
        try:
            while True:
                frame = parse_frame(await self._ws.recv())
                if frame["type"] == "output_unit":
                    status, detail = await self._on_unit(frame["payload"])
                    await self.send_result(status, detail)
                elif frame["type"] == "status_query":
                    reply = make_frame(
                        "status_result",
                        self._name,
                        self._status_provider(),
                        request_id=frame["request_id"],
                    )
                    await self._ws.send(dumps(reply))
                else:
                    LOGGER.debug("ignoring frame type %s", frame["type"])
        except (websockets.ConnectionClosed, ConnectionError, OSError):
            LOGGER.debug("hub connection closed (reader)")
        except Exception as exc:  # noqa: BLE001 - reader 最后防线：任何异常记日志后退出并置 _closed，由重连接管
            LOGGER.warning("hub reader error: %s", exc)
        finally:
            self._closed.set()

    async def send_result(self, status: str, detail: str) -> None:
        if self._ws is None:
            raise RuntimeError("send_result before __aenter__")
        payload = {"module": self._name, "status": status, "detail": detail}
        await self._ws.send(dumps(make_frame("result", self._name, payload)))

    async def send_input(
        self,
        content: str,
        *,
        source: str = MODULE_NAME,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._ws is None:
            raise RuntimeError("send_input before __aenter__")
        payload = {"content": content, "source": source, "metadata": metadata or {}}
        await self._ws.send(dumps(make_frame("input", self._name, payload)))


__all__ = ["HubConnectionError", "LumenClient", "OnUnit"]
