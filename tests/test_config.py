"""Environment loading and validation (mirrors the voice-input precedent)."""
from __future__ import annotations

import pytest

from presenter.config import ConfigError, load_settings


def _env(**overrides: str) -> dict[str, str]:
    base = {"WAYCAST_HUB_URL": "ws://127.0.0.1:8000"}
    base.update(overrides)
    return base


def test_defaults() -> None:
    settings = load_settings(_env())
    assert settings.hub_url == "ws://127.0.0.1:8000"
    assert settings.tts_engine == "auto"
    assert settings.cosyvoice_model == "cosyvoice-v3-flash"
    assert settings.bridge_port == 8790
    assert settings.queue_max == 64


def test_hub_url_required_and_ws_scheme() -> None:
    with pytest.raises(ConfigError):
        load_settings({})
    with pytest.raises(ConfigError):
        load_settings(_env(WAYCAST_HUB_URL="http://127.0.0.1:8000"))


def test_engine_choice_validation() -> None:
    assert load_settings(_env(PRESENTER_TTS_ENGINE="cosyvoice")).tts_engine == "cosyvoice"
    assert load_settings(_env(PRESENTER_TTS_ENGINE="audio_cpp")).tts_engine == "audio_cpp"
    with pytest.raises(ConfigError):
        load_settings(_env(PRESENTER_TTS_ENGINE="kittyvoice"))


def test_audio_cpp_settings() -> None:
    settings = load_settings(
        _env(
            PRESENTER_AUDIO_CPP_URL="http://127.0.0.1:9999/",
            PRESENTER_AUDIO_CPP_MODEL="qwen3-tts",
            PRESENTER_AUDIO_CPP_VOICE_REF="/voices/ref.wav",
        )
    )
    assert settings.audio_cpp_url == "http://127.0.0.1:9999"  # trailing slash trimmed
    assert settings.audio_cpp_model == "qwen3-tts"
    assert settings.audio_cpp_sample_rate == 24_000
    assert settings.tts_fallback_after == 3
    assert settings.tts_retry_after_seconds == 60.0
    with pytest.raises(ConfigError):
        load_settings(_env(PRESENTER_AUDIO_CPP_URL="ftp://127.0.0.1"))
    with pytest.raises(ConfigError):
        load_settings(_env(PRESENTER_TTS_RETRY_AFTER_SECONDS="0.5"))


def test_int_parsing() -> None:
    with pytest.raises(ConfigError):
        load_settings(_env(PRESENTER_QUEUE_MAX="many"))
    with pytest.raises(ConfigError):
        load_settings(_env(PRESENTER_BRIDGE_PORT="0"))
