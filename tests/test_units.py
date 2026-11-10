"""Unit schema (as the hub validates it pre-dispatch, A22) and unit parsing."""

from __future__ import annotations

import jsonschema
import pytest

from presenter.schema import OUTPUT_SCHEMA
from presenter.units import PptCommand, UnitError, parse_unit


def _validate(payload: dict) -> None:
    jsonschema.validate(payload, OUTPUT_SCHEMA)


def test_accepted_payloads() -> None:
    for payload in (
        {"speech": "第一句。"},
        {"speech": "看这里。", "emotion": "wink", "motion": "wave"},
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
    unit = parse_unit({"speech": "你好呀", "emotion": "wink"})
    assert unit.speech == "你好呀"
    assert unit.subtitle_text == "你好呀"
    assert unit.visual_tags() == {"emotion": "wink"}
    overridden = parse_unit({"speech": "朗读文本", "subtitle": "显示文本"})
    assert overridden.subtitle_text == "显示文本"


def test_parse_stop_and_guards() -> None:
    assert parse_unit({"control": "stop"}).is_stop
    with pytest.raises(UnitError):
        parse_unit(
            {"subtitle": "只有字幕没有可执行字段"}
        )  # speech/emotion/motion absent
    with pytest.raises(UnitError):
        parse_unit({"control": "bogus"})


def test_ppt_schema() -> None:
    for payload in (
        {"ppt": {"action": "next"}},
        {"ppt": {"action": "open_deck", "deck": "intro-1"}},
        {"ppt": {"action": "goto", "page": 12}},
        {"speech": "看这页。", "ppt": {"action": "next"}},
    ):
        _validate(payload)
    for payload in (
        {"ppt": {"action": "goto"}},  # goto requires page
        {"ppt": {"action": "open_deck"}},  # open_deck requires deck
        {"ppt": {"action": "flip"}},  # enum
        {"ppt": {"action": "next", "slide": 3}},  # additionalProperties false
        {"ppt": {"action": "open_deck", "deck": "../etc"}},  # safe-id pattern
        {"ppt": {"action": "goto", "page": 0}},  # minimum
    ):
        with pytest.raises(jsonschema.ValidationError):
            _validate(payload)


def test_parse_ppt() -> None:
    unit = parse_unit(
        {"speech": "第一页。", "ppt": {"action": "open_deck", "deck": "demo"}}
    )
    assert unit.ppt == PptCommand(action="open_deck", deck="demo", page=None)
    ppt_only = parse_unit({"ppt": {"action": "close"}})  # ppt alone is executable
    assert ppt_only.speech is None and ppt_only.ppt is not None
    with pytest.raises(UnitError):
        parse_unit({"ppt": {"action": "goto"}})
    with pytest.raises(UnitError):
        parse_unit({"ppt": {"action": "next", "page": 0}})
