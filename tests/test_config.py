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
    with pytest.raises(ConfigError):
        load_settings(_env(PRESENTER_TTS_ENGINE="kittyvoice"))
    with pytest.raises(ConfigError):
        # audio_cpp only lands in M4 (ADR-0001)
        load_settings(_env(PRESENTER_TTS_ENGINE="audio_cpp"))


def test_int_parsing() -> None:
    with pytest.raises(ConfigError):
        load_settings(_env(PRESENTER_QUEUE_MAX="many"))
    with pytest.raises(ConfigError):
        load_settings(_env(PRESENTER_BRIDGE_PORT="0"))
