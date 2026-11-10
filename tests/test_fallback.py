"""AutoTtsEngine degradation: transparent retry, threshold, cooldown probe."""

from __future__ import annotations

import asyncio

from lumen.fallback import AutoTtsEngine
from lumen.tts import TtsError
from placeholders import ScriptedEngine


def _run(engine: AutoTtsEngine, text: str = "句子。") -> tuple[bytes, TtsError | None]:
    async def go() -> tuple[bytes, TtsError | None]:
        out = bytearray()
        try:
            async for chunk in engine.stream(text):
                out.extend(chunk)
        except TtsError as exc:
            return bytes(out), exc
        return bytes(out), None

    return asyncio.run(go())


def _auto(
    primary_script: list[str],
    fallback_script: list[str],
    *,
    fail_after: int = 2,
    retry_after: float = 10.0,
) -> tuple[AutoTtsEngine, ScriptedEngine, ScriptedEngine, list[float]]:
    now = [0.0]
    primary = ScriptedEngine("cosyvoice", primary_script)
    fallback = ScriptedEngine("audio_cpp", fallback_script)
    engine = AutoTtsEngine(
        primary,
        fallback,
        fail_after=fail_after,
        retry_after_seconds=retry_after,
        clock=lambda: now[0],  # type: ignore[arg-type]
    )
    return engine, primary, fallback, now


def test_pre_audio_failure_retries_transparently() -> None:
    engine, primary, fallback, _ = _auto(["fail"], ["ok"])
    audio, error = _run(engine)
    assert error is None
    assert audio.startswith(b"audio_cpp:")
    assert (primary.calls, fallback.calls) == (1, 1)
    assert engine.name == "cosyvoice", "one failure must not flip the active engine"


def test_mid_stream_failure_is_not_retried() -> None:
    engine, _primary, fallback, _ = _auto(["mid"], ["ok"])
    audio, error = _run(engine)
    assert error is not None
    assert audio.startswith(b"cosyvoice:")
    assert fallback.calls == 0, "audio already played must not be doubled"


def test_threshold_degrades_and_cooldown_probes_recovery() -> None:
    engine, primary, fallback, now = _auto(
        ["fail", "fail", "ok", "ok", "ok"], ["ok", "ok", "ok"]
    )
    _run(engine)  # failure 1 -> transparent retry on fallback
    _run(engine)  # failure 2 -> degrade
    assert engine.name == "audio_cpp"
    assert primary.calls == 2

    _run(engine)  # degraded: fallback only, no cloud call
    assert (primary.calls, fallback.calls) == (2, 3)

    now[0] = 10.0  # cooldown elapsed -> this sentence probes the cloud first
    _run(engine)
    assert primary.calls == 3
    assert engine.name == "cosyvoice", "successful probe restores the primary"

    audio, error = _run(engine)
    assert error is None
    assert audio.startswith(b"cosyvoice:")
    assert fallback.calls == 3, "restored primary no longer touches the fallback"


def test_failed_probe_keeps_engine_degraded() -> None:
    engine, primary, _fallback, now = _auto(
        ["fail", "fail", "fail", "fail"], ["ok", "ok", "ok", "ok"]
    )
    _run(engine)
    _run(engine)
    assert engine.name == "audio_cpp"
    now[0] = 10.0
    audio, error = _run(engine)  # probe fails, transparent retry saves the sentence
    assert error is None and audio.startswith(b"audio_cpp:")
    assert engine.name == "audio_cpp"
    now[0] = 15.0  # only 5 s since the new failure: still cooling down
    _run(engine)
    assert primary.calls == 3, "probe must wait out the full cooldown"


def test_without_fallback_failures_just_raise() -> None:
    primary = ScriptedEngine("cosyvoice", ["fail"])
    engine = AutoTtsEngine(primary, None, fail_after=1)
    _, error = _run(engine)
    assert isinstance(error, TtsError)
    assert engine.name == "cosyvoice"
