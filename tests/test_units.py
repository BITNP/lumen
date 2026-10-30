"""Unit schema (as the hub validates it pre-dispatch, A22) and unit parsing."""
from __future__ import annotations

import jsonschema
import pytest

from presenter.schema import OUTPUT_SCHEMA
from presenter.units import UnitError, parse_unit


def _validate(payload: dict) -> None:
    jsonschema.validate(payload, OUTPUT_SCHEMA)


def test_accepted_payloads() -> None:
    for payload in (
        {"speech": "第一句。"},
        {"speech": "看这里。", "emotion": "happy", "motion": "wave"},
        {"subtitle": "只显示的字幕"},
        {"control": "stop"},
    ):
        _validate(payload)


def test_rejected_payloads() -> None:
    for payload in (
        {},  # minProperties
        {"speech": ""},  # minLength
        {"control": "replace"},  # enum: only stop exists (ADR-0003)
        {"text": "wrong field"},  # additionalProperties false
    ):
        with pytest.raises(jsonschema.ValidationError):
            _validate(payload)


def test_parse_defaults_and_subtitle_fallback() -> None:
    unit = parse_unit({"speech": "你好呀", "emotion": "happy"})
    assert unit.speech == "你好呀"
    assert unit.subtitle_text == "你好呀"
    assert unit.visual_tags() == {"emotion": "happy"}
    overridden = parse_unit({"speech": "朗读文本", "subtitle": "显示文本"})
    assert overridden.subtitle_text == "显示文本"


def test_parse_stop_and_guards() -> None:
    assert parse_unit({"control": "stop"}).is_stop
    with pytest.raises(UnitError):
        parse_unit({"subtitle": "只有字幕没有可执行字段"})  # speech/emotion/motion absent
    with pytest.raises(UnitError):
        parse_unit({"control": "bogus"})
