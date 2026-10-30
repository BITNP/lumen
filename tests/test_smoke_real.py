"""Real-service smoke tests (workspace rule: real synthesis, never a fake).

Run on the dev machine:

    uv run --locked pytest -q -m real

Requires in the environment (or .env): PRESENTER_DASHSCOPE_API_KEY and
PRESENTER_COSYVOICE_VOICE (cloned voice id).

Full end-to-end acceptance for M1 (manual, real model):
1. Start the waycast core (see waycast README) with a module token.
2. Start this host: `uv run --locked presenter`; optionally `godot --path godot/`.
3. Start voice input or send a text input to the hub; the Agent should answer
   sentence-per-unit; the machine speaks, the window reveals subtitles on the
   playback clock; after the last sentence a queue_finished input arrives and
   the Agent stays silent (nothing more to say).
4. Interrupt check: send a new question mid-speech; the Agent emits
   {"control":"stop"} + new sentences; old audio stops within ~1 chunk.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from presenter.config import ConfigError, load_settings

pytestmark = pytest.mark.real


def _settings():  # noqa: ANN202
    try:
        return load_settings()
    except ConfigError as exc:
        pytest.skip(f"real smoke needs presenter env: {exc}")


def test_cosyvoice_real_stream_produces_pcm() -> None:
    from presenter.app import build_engine

    settings = _settings()
    if not settings.dashscope_api_key or not settings.cosyvoice_voice:
        pytest.skip("PRESENTER_DASHSCOPE_API_KEY / PRESENTER_COSYVOICE_VOICE not configured")
    engine = build_engine(settings)

    async def run() -> bytes:
        audio = bytearray()
        async for chunk in engine.stream("你好，我是树莓娘，很高兴见到你。"):
            audio.extend(chunk)
        return bytes(audio)

    audio = asyncio.run(run())
    # A spoken sentence is well over a second of 16 kHz mono s16le.
    assert len(audio) > 32_000
    assert len(audio) % 2 == 0


def test_real_playback_device_available() -> None:
    sounddevice = pytest.importorskip("sounddevice")
    try:
        default = sounddevice.default.device
    except Exception as exc:  # PortAudio not usable at all
        pytest.skip(f"no audio subsystem: {exc}")
    if default is None or default[1] < 0:
        pytest.skip("no default output device on this machine")
    assert os.name == "posix"


def test_audio_cpp_real_stream() -> None:
    """Real audio.cpp server (external service): start audiocpp_server first."""
    from presenter.app import _audio_cpp_engine

    settings = _settings()
    if not (settings.audio_cpp_model and settings.audio_cpp_voice_ref):
        pytest.skip("PRESENTER_AUDIO_CPP_MODEL / PRESENTER_AUDIO_CPP_VOICE_REF not set")
    engine = _audio_cpp_engine(settings)

    async def run() -> bytes | None:
        if not await engine.health():
            return None
        audio = bytearray()
        async for chunk in engine.stream("你好，我是树莓娘。"):
            audio.extend(chunk)
        await engine.close()
        return bytes(audio)

    audio = asyncio.run(run())
    if audio is None:
        pytest.skip(f"audiocpp_server not healthy at {settings.audio_cpp_url}")
    assert len(audio) > 16_000
    assert len(audio) % 2 == 0
