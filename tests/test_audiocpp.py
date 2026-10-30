"""audio.cpp engine against a tests/ SSE server that mirrors app/server/api.md
verbatim (speech.audio.delta / speech.audio.done / [DONE]), plus the resampler.

The real-service behaviour is covered by the real-marked smoke test; this file
locks the HTTP/SSE contract framing and the 24k -> 16k resampling.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any

import pytest

from presenter.audiocpp import AudioCppEngine, _LinearResampler
from presenter.tts import TtsError

PCM_24K = b"\x00\x64" * 120  # 120 samples of 24 kHz s16le


def _sse_bytes(events: list[dict[str, Any]], *, done: bool = True) -> bytes:
    lines = []
    for event in events:
        lines.append(f"data: {json.dumps(event)}\n\n")
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def _server(scope: dict[str, Any], status: str, body: bytes) -> Any:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        match = re.search(rb"content-length: (\d+)", head.lower())
        assert match is not None
        scope["body"] = json.loads(await reader.readexactly(int(match.group(1))))
        scope["head"] = head.decode()
        writer.write(
            f"HTTP/1.1 {status}\r\ncontent-type: text/event-stream\r\n"
            f"connection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    return asyncio.start_server(handler, "127.0.0.1", 0)


def _engine(port: int, **kwargs: Any) -> AudioCppEngine:
    return AudioCppEngine(
        base_url=f"http://127.0.0.1:{port}",
        model="qwen3-tts",
        voice_ref="/voices/ref.wav",
        reference_text="参考音频文字稿。",
        **kwargs,
    )


def test_streams_resampled_pcm_and_posts_documented_body() -> None:
    scope: dict[str, Any] = {}
    body = _sse_bytes(
        [
            {"type": "speech.audio.delta", "audio": base64.b64encode(PCM_24K).decode()},
            {"type": "speech.audio.delta", "audio": base64.b64encode(PCM_24K).decode()},
            {"type": "speech.audio.done", "timing": {"ttft_ms": 12}},
        ]
    )

    async def scenario() -> bytes:
        server = await _server(scope, "200 OK", body)
        port = server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        await asyncio.sleep(0)
        engine = _engine(port)
        audio = bytearray()
        async for chunk in engine.stream("你好。"):
            audio.extend(chunk)
        await engine.close()
        server.close()
        await server.wait_closed()
        return bytes(audio)

    audio = asyncio.run(scenario())
    # 240 samples in at 24 kHz -> 160 samples out at 16 kHz = 320 bytes.
    assert len(audio) == 320
    assert scope["body"] == {
        "model": "qwen3-tts",
        "input": "你好。",
        "response_format": "pcm",
        "stream_format": "sse",
        "voice_ref": {"type": "path", "path": "/voices/ref.wav"},
        "reference_text": "参考音频文字稿。",
    }
    assert "text/event-stream" in scope["head"]


def test_without_reference_text_requests_x_vector_only() -> None:
    scope: dict[str, Any] = {}
    body = _sse_bytes(
        [{"type": "speech.audio.delta", "audio": base64.b64encode(PCM_24K).decode()}]
    )

    async def scenario() -> None:
        server = await _server(scope, "200 OK", body)
        port = server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        engine = AudioCppEngine(
            base_url=f"http://127.0.0.1:{port}",
            model="qwen3-tts",
            voice_ref="/voices/ref.wav",
            sample_rate=24_000,
        )
        async for _ in engine.stream("喂。"):
            pass
        await engine.close()
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())
    assert scope["body"]["options"] == {"x_vector_only_mode": True}
    assert "reference_text" not in scope["body"]


def test_http_error_raises_tts_error() -> None:
    scope: dict[str, Any] = {}

    async def scenario() -> None:
        server = await _server(scope, "500 Internal Server Error", b'{"error":"boom"}')
        port = server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        engine = _engine(port)
        with pytest.raises(TtsError, match="HTTP 500"):
            async for _ in engine.stream("测试。"):
                pass
        await engine.close()
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())


def test_malformed_base64_delta_raises() -> None:
    scope: dict[str, Any] = {}
    body = _sse_bytes([{"type": "speech.audio.delta", "audio": "!!!not base64!!!"}])

    async def scenario() -> None:
        server = await _server(scope, "200 OK", body)
        port = server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        engine = _engine(port)
        with pytest.raises(TtsError, match="base64"):
            async for _ in engine.stream("测试。"):
                pass
        await engine.close()
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())


def test_empty_stream_raises() -> None:
    scope: dict[str, Any] = {}

    async def scenario() -> None:
        server = await _server(scope, "200 OK", _sse_bytes([]))
        port = server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        engine = _engine(port)
        with pytest.raises(TtsError, match="no audio"):
            async for _ in engine.stream("测试。"):
                pass
        await engine.close()
        server.close()
        await server.wait_closed()

    asyncio.run(scenario())


def test_linear_resampler_preserves_value_and_length() -> None:
    resampler = _LinearResampler(24_000, 16_000)
    constant = (1000).to_bytes(2, "little", signed=True) * 240
    out = resampler.feed(constant) + resampler.flush()
    assert len(out) == 320  # 240 samples -> 160 samples
    values = {int.from_bytes(out[i : i + 2], "little", signed=True) for i in range(0, 320, 2)}
    assert values == {1000}
