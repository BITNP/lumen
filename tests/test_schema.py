"""The advertised contract cannot outpace what the avatar can perform.

The hub injects OUTPUT_SCHEMA into the system prompt and validates every
dispatched unit against it, so emotion/motion enums must equal the frontend
whitelists (godot/scripts/live2d_avatar.gd) and the assets those lists name.
"""
from __future__ import annotations

import re
from pathlib import Path

import jsonschema
import pytest

from presenter.schema import EMOTION_TAGS, MOTION_TAGS, OUTPUT_SCHEMA

GODOT = Path(__file__).resolve().parents[1] / "godot"


def _gd_const(name: str) -> tuple[str, ...]:
    source = (GODOT / "scripts" / "live2d_avatar.gd").read_text(encoding="utf-8")
    match = re.search(rf"const {name} := \[([^\]]*)\]", source)
    assert match, f"const {name} not found in live2d_avatar.gd"
    return tuple(re.findall(r'"([^"]+)"', match.group(1)))


def test_emotion_enum_matches_frontend_expression_whitelist() -> None:
    assert list(EMOTION_TAGS) == list(_gd_const("EXPRESSIONS"))


def test_motion_enum_matches_frontend_motion_whitelist() -> None:
    assert list(MOTION_TAGS) == list(_gd_const("MOTIONS"))


def test_expression_clips_exist_for_every_emotion_tag() -> None:
    clips = {
        p.stem for p in (GODOT / "assets" / "face_expressions").glob("*.json")
    }
    assert set(EMOTION_TAGS) == clips


def test_motion_files_exist_for_every_motion_tag() -> None:
    motions = {
        p.name.split(".", 1)[0]
        for p in (GODOT / "assets" / "DAver3.0" / "motion").glob("*.motion3.json")
    }
    assert set(MOTION_TAGS) == motions


def test_schema_rejects_tags_the_avatar_cannot_perform() -> None:
    jsonschema.validate({"emotion": "wink", "motion": "wave"}, OUTPUT_SCHEMA)
    for unit in ({"emotion": "happy"}, {"motion": "nod"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(unit, OUTPUT_SCHEMA)
