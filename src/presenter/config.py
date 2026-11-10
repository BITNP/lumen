"""Runtime configuration for the presenter (aggregate output module).

All values come from the environment (the .env file is loaded first by main).
Hub connection settings mirror the waycast module contract; everything
module-local is prefixed PRESENTER_.  Credentials only ever live in the local
environment, never in source, docs, commits or logs.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

TtsEngineChoice = Literal["auto", "cosyvoice", "audio_cpp"]

WAYCAST_HUB_URL_KEY: Final = "WAYCAST_HUB_URL"
WAYCAST_HUB_TOKEN_KEY: Final = "WAYCAST_HUB_TOKEN"
WAYCAST_TLS_CA_KEY: Final = "WAYCAST_TLS_CA"
TTS_ENGINE_KEY: Final = "PRESENTER_TTS_ENGINE"
DASHSCOPE_API_KEY_KEY: Final = "PRESENTER_DASHSCOPE_API_KEY"
COSYVOICE_MODEL_KEY: Final = "PRESENTER_COSYVOICE_MODEL"
COSYVOICE_VOICE_KEY: Final = "PRESENTER_COSYVOICE_VOICE"
AUDIO_CPP_URL_KEY: Final = "PRESENTER_AUDIO_CPP_URL"
AUDIO_CPP_MODEL_KEY: Final = "PRESENTER_AUDIO_CPP_MODEL"
AUDIO_CPP_VOICE_REF_KEY: Final = "PRESENTER_AUDIO_CPP_VOICE_REF"
AUDIO_CPP_REFERENCE_TEXT_KEY: Final = "PRESENTER_AUDIO_CPP_REFERENCE_TEXT"
AUDIO_CPP_SAMPLE_RATE_KEY: Final = "PRESENTER_AUDIO_CPP_SAMPLE_RATE"
TTS_FALLBACK_AFTER_KEY: Final = "PRESENTER_TTS_FALLBACK_AFTER"
TTS_RETRY_AFTER_KEY: Final = "PRESENTER_TTS_RETRY_AFTER_SECONDS"
AUDIO_DEVICE_KEY: Final = "PRESENTER_AUDIO_DEVICE"
BRIDGE_PORT_KEY: Final = "PRESENTER_BRIDGE_PORT"
GODOT_PATH_KEY: Final = "PRESENTER_GODOT_PATH"
QUEUE_MAX_KEY: Final = "PRESENTER_QUEUE_MAX"
DECKS_DIR_KEY: Final = "PRESENTER_DECKS_DIR"
SENTENCE_GAP_KEY: Final = "PRESENTER_SENTENCE_GAP_SECONDS"

DEFAULT_TTS_ENGINE: Final = "auto"
DEFAULT_COSYVOICE_MODEL: Final = "cosyvoice-v3-flash"
DEFAULT_BRIDGE_PORT: Final = 8790
DEFAULT_QUEUE_MAX: Final = 64
# audio.cpp serves both ASR and TTS on this machine at the standard port.
DEFAULT_AUDIO_CPP_URL: Final = "http://127.0.0.1:8080"
DEFAULT_AUDIO_CPP_SAMPLE_RATE: Final = 24_000
DEFAULT_TTS_FALLBACK_AFTER: Final = 3
DEFAULT_TTS_RETRY_AFTER_SECONDS: Final = 60.0
# Minimum gap between sentences: the next sentence opens no sooner than this
# after the previous one fully played.  The synthesis wait counts into the gap
# (never stacked on top of it) and the round's first sentence never waits.
DEFAULT_SENTENCE_GAP_SECONDS: Final = 1.0


@dataclass(frozen=True, slots=True)
class Settings:
    hub_url: str
    hub_token: str
    tts_engine: TtsEngineChoice
    dashscope_api_key: str | None
    cosyvoice_model: str
    cosyvoice_voice: str | None
    tls_ca_path: str | None = None
    audio_device: str | None = None
    godot_path: str | None = None
    bridge_port: int = DEFAULT_BRIDGE_PORT
    queue_max: int = DEFAULT_QUEUE_MAX
    decks_dir: str | None = None
    audio_cpp_url: str = DEFAULT_AUDIO_CPP_URL
    audio_cpp_model: str | None = None
    audio_cpp_voice_ref: str | None = None
    audio_cpp_reference_text: str | None = None
    audio_cpp_sample_rate: int = DEFAULT_AUDIO_CPP_SAMPLE_RATE
    tts_fallback_after: int = DEFAULT_TTS_FALLBACK_AFTER
    tts_retry_after_seconds: float = DEFAULT_TTS_RETRY_AFTER_SECONDS
    sentence_gap_seconds: float = DEFAULT_SENTENCE_GAP_SECONDS


class ConfigError(RuntimeError):
    """Raised for a missing or invalid environment setting."""

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"{key}: {reason}")
        self.key = key
        self.reason = reason


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Read every setting from the environment with validation.

    Pass an explicit mapping (e.g. os.environ or a fixture) instead of a live
    process environment when testing.
    """
    source = os.environ if env is None else env
    hub_url = _required(source, WAYCAST_HUB_URL_KEY)
    _require_ws_scheme(hub_url, WAYCAST_HUB_URL_KEY)
    engine = (
        source.get(TTS_ENGINE_KEY, DEFAULT_TTS_ENGINE).strip() or DEFAULT_TTS_ENGINE
    )
    if engine not in ("auto", "cosyvoice", "audio_cpp"):
        raise ConfigError(TTS_ENGINE_KEY, "must be auto, cosyvoice or audio_cpp")
    return Settings(
        hub_url=hub_url,
        hub_token=source.get(WAYCAST_HUB_TOKEN_KEY, "").strip(),
        tts_engine=engine,  # validated literal above
        dashscope_api_key=_optional_text(source.get(DASHSCOPE_API_KEY_KEY)),
        cosyvoice_model=source.get(COSYVOICE_MODEL_KEY, "").strip()
        or DEFAULT_COSYVOICE_MODEL,
        cosyvoice_voice=_optional_text(source.get(COSYVOICE_VOICE_KEY)),
        tls_ca_path=_optional_text(source.get(WAYCAST_TLS_CA_KEY)),
        audio_device=_optional_text(source.get(AUDIO_DEVICE_KEY)),
        godot_path=_optional_text(source.get(GODOT_PATH_KEY)),
        bridge_port=_parse_int(source, BRIDGE_PORT_KEY, DEFAULT_BRIDGE_PORT, minimum=1),
        queue_max=_parse_int(source, QUEUE_MAX_KEY, DEFAULT_QUEUE_MAX, minimum=1),
        decks_dir=_optional_text(source.get(DECKS_DIR_KEY)),
        audio_cpp_url=_audio_cpp_url(source),
        audio_cpp_model=_optional_text(source.get(AUDIO_CPP_MODEL_KEY)),
        audio_cpp_voice_ref=_optional_text(source.get(AUDIO_CPP_VOICE_REF_KEY)),
        audio_cpp_reference_text=_optional_text(
            source.get(AUDIO_CPP_REFERENCE_TEXT_KEY)
        ),
        audio_cpp_sample_rate=_parse_int(
            source,
            AUDIO_CPP_SAMPLE_RATE_KEY,
            DEFAULT_AUDIO_CPP_SAMPLE_RATE,
            minimum=8_000,
        ),
        tts_fallback_after=_parse_int(
            source, TTS_FALLBACK_AFTER_KEY, DEFAULT_TTS_FALLBACK_AFTER, minimum=1
        ),
        tts_retry_after_seconds=_parse_float(
            source, TTS_RETRY_AFTER_KEY, DEFAULT_TTS_RETRY_AFTER_SECONDS, minimum=1.0
        ),
        sentence_gap_seconds=_parse_float(
            source, SENTENCE_GAP_KEY, DEFAULT_SENTENCE_GAP_SECONDS, minimum=0.0
        ),
    )


def _audio_cpp_url(source: Mapping[str, str]) -> str:
    value = source.get(AUDIO_CPP_URL_KEY, "").strip() or DEFAULT_AUDIO_CPP_URL
    if not value.startswith(("http://", "https://")):
        raise ConfigError(AUDIO_CPP_URL_KEY, "must be an http:// or https:// URL")
    return value.rstrip("/")


def _required(source: Mapping[str, str], key: str) -> str:
    value = source.get(key, "").strip()
    if not value:
        raise ConfigError(key, "required setting is missing or empty")
    return value


def _optional_text(value: str | None) -> str | None:
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _require_ws_scheme(value: str, key: str) -> None:
    if not value.startswith(("ws://", "wss://")):
        raise ConfigError(key, "must be a ws:// or wss:// URL")


def _parse_int(
    source: Mapping[str, str], key: str, default: int, *, minimum: int
) -> int:
    raw = source.get(key, "").strip()
    if raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(key, "must be an integer") from exc
    if value < minimum:
        raise ConfigError(key, f"must be >= {minimum}")
    return value


def _parse_float(
    source: Mapping[str, str], key: str, default: float, *, minimum: float
) -> float:
    raw = source.get(key, "").strip()
    if raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(key, "must be a number") from exc
    if value < minimum:
        raise ConfigError(key, f"must be >= {minimum}")
    return value


__all__ = ["Settings", "ConfigError", "load_settings", "TtsEngineChoice"]
