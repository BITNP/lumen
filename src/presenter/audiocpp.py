"""audio.cpp engine (local fallback per ADR-0001).

Plain HTTP against the audiocpp_server OpenAI-style route; the authoritative
shape is app/server/api.md in the local audio.cpp checkout:
POST /v1/audio/speech with response_format=pcm and stream_format=sse, SSE
events speech.audio.delta (base64 PCM), speech.audio.done, then [DONE].
Voice cloning rides on voice_ref (a server-readable WAV path) plus
reference_text, the zero-shot clone flow used by the audio8_tts family;
without reference_text the request sets options.x_vector_only_mode.

The server streams at the model's native rate (audio8_tts: 44.1 kHz,
Qwen3-TTS: 24 kHz), so chunks are linearly resampled down to the module-wide
16 kHz TtsEngine contract. audio.cpp is an external service: the host probes
/health but never spawns it.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any, cast

import httpx2

from presenter.tts import SAMPLE_RATE_HZ, TtsError

LOGGER = logging.getLogger(__name__)


class _LinearResampler:
    """Streaming s16le mono resampler with linear interpolation."""

    def __init__(self, in_rate: int, out_rate: int) -> None:
        self._step = in_rate / out_rate
        self._samples: list[int] = []
        self._position = 0.0
        self._leftover = b""

    def feed(self, chunk: bytes) -> bytes:
        data = self._leftover + chunk
        if len(data) % 2:
            self._leftover = data[-1:]
            data = data[:-1]
        else:
            self._leftover = b""
        self._samples.extend(
            int.from_bytes(data[i : i + 2], "little", signed=True)
            for i in range(0, len(data), 2)
        )
        out: list[int] = []
        while self._position + 1.0 < len(self._samples):
            index = int(self._position)
            fraction = self._position - index
            value = (
                self._samples[index]
                + (self._samples[index + 1] - self._samples[index]) * fraction
            )
            out.append(max(-32_768, min(32_767, round(value))))
            self._position += self._step
        consumed = int(self._position)
        if consumed:
            del self._samples[:consumed]
            self._position -= consumed
        return b"".join(value.to_bytes(2, "little", signed=True) for value in out)

    def flush(self) -> bytes:
        if len(self._leftover) == 1:
            self._leftover += b"\x00"
            out = self.feed(self._leftover)
            self._leftover = b""
            return out
        return b""


class AudioCppEngine:
    """One sentence per stream() call via SSE; shared httpx2 client underneath."""

    name = "audio_cpp"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        voice_ref: str,
        reference_text: str | None = None,
        sample_rate: int = 24_000,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._voice_ref = voice_ref
        self._reference_text = reference_text
        self._sample_rate_hz = sample_rate
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            timeout=httpx2.Timeout(timeout_seconds, connect=10.0, read=timeout_seconds),
            # audio.cpp is a localhost service: ambient proxy env vars (HTTP_PROXY,
            # ALL_PROXY socks://...) must never intercept or break these calls.
            trust_env=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        """True when the audio.cpp server answers /health (external service)."""
        try:
            response = await self._client.get("/health", timeout=5.0)
            return response.status_code == 200
        except httpx2.HTTPError:
            return False

    async def stream(self, text: str) -> AsyncGenerator[bytes, None]:
        payload: dict[str, Any] = {
            "model": self._model,
            "input": text,
            "response_format": "pcm",
            "stream_format": "sse",
            "voice_ref": {"type": "path", "path": self._voice_ref},
        }
        if self._reference_text:
            payload["reference_text"] = self._reference_text
        else:
            payload["options"] = {"x_vector_only_mode": True}
        resampler = (
            _LinearResampler(self._sample_rate_hz, SAMPLE_RATE_HZ)
            if self._sample_rate_hz != SAMPLE_RATE_HZ
            else None
        )
        produced = 0
        try:
            # httpx2 原生 SSE（client.sse）：帧切分/解码交给库，[DONE] 哨兵按
            # OpenAI 约定是 data 载荷而非 JSON 事件。max_event_size=None 保持
            # 旧手写实现的无上限语义（大 delta 不应被拒收）。
            async with self._client.sse(
                "/v1/audio/speech",
                method="POST",
                json=payload,
                max_event_size=None,
            ) as events:
                response = events.response
                if response.status_code != 200:
                    await response.aread()
                    raise TtsError(f"audio.cpp HTTP {response.status_code}")
                async for event in events:
                    if event.data.strip() == "[DONE]":
                        break
                    frame = _parse_sse_data(event.data)
                    kind = frame.get("type")
                    if kind == "speech.audio.delta":
                        chunk = _decode_delta(frame)
                        produced += len(chunk)
                        out = resampler.feed(chunk) if resampler else chunk
                        if out:
                            yield out
                    elif kind == "speech.audio.done":
                        break
                    elif isinstance(kind, str) and "error" in kind:
                        raise TtsError(f"audio.cpp stream error: {kind}")
        except TtsError:
            raise
        except httpx2.HTTPError as exc:
            raise TtsError(f"audio.cpp request failed: {type(exc).__name__}") from exc
        if resampler:
            tail = resampler.flush()
            produced += len(tail)
            if tail:
                yield tail
        if produced == 0:
            raise TtsError("audio.cpp produced no audio")


def _parse_sse_data(data: str) -> dict[str, Any]:
    try:
        parsed: object = json.loads(data)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise TtsError(f"audio.cpp malformed SSE frame: {exc}") from exc
    if not isinstance(parsed, dict):
        raise TtsError("audio.cpp SSE frame is not an object")
    return cast("dict[str, Any]", parsed)


def _decode_delta(event: dict[str, Any]) -> bytes:
    raw = event.get("audio")
    if not isinstance(raw, str) or not raw:
        raise TtsError("audio.cpp delta without audio")
    try:
        pcm = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TtsError("audio.cpp delta is not base64") from exc
    if len(pcm) % 2:
        raise TtsError("audio.cpp delta has odd PCM length")
    return pcm


__all__ = ["AudioCppEngine"]
