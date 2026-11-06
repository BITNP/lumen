"""The advertised contract cannot outpace what the avatar can perform.

The hub injects OUTPUT_SCHEMA into the system prompt and validates every
dispatched unit against it, so emotion/motion enums must equal the frontend
whitelists (godot/scripts/live2d_avatar.gd) and the assets those lists name.
"""
from __future__ import annotations

import json
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
    # "reset" is the driver-side recovery motion (see live2d_avatar.gd), not
    # dispatchable by the Agent.
    assert set(MOTION_TAGS) == motions - {"reset"}


def test_reset_motion_covers_every_gesture_curve_target() -> None:
    """reset.motion3.json must fade back every curve the gestures animate.

    Cubism only persists values written inside a motion update, so a gesture
    curve missing from the reset file would leave its parameter stuck at the
    gesture ending (the sweat=1 bug). Baselines live in the moc; this guards
    target coverage only.
    """
    motion_dir = GODOT / "assets" / "DAver3.0" / "motion"
    gestures = set(MOTION_TAGS)
    targets: set[tuple[str, str]] = set()
    for name in gestures:
        asset = json.loads(
            (motion_dir / f"{name}.motion3.json").read_text(encoding="utf-8")
        )
        for curve in asset["Curves"]:
            # Model-target curves (EyeBlink/LipSync/Opacity) are handler
            # channels, not parameters; mouth and breath are host/script
            # driven. None of them take part in the motion-recovery blend.
            if curve["Target"] == "Model":
                continue
            if curve["Target"] == "Parameter" and curve["Id"] in (
                "ParamMouthOpenY",
                "ParamBreath",
            ):
                continue
            targets.add((curve["Target"], curve["Id"]))
    reset = json.loads(
        (motion_dir / "reset.motion3.json").read_text(encoding="utf-8")
    )
    reset_targets = {(c["Target"], c["Id"]) for c in reset["Curves"]}
    assert targets <= reset_targets

    # Cubism pre-allocates its segment/point buffers from Meta counts and
    # writes through them unchecked: a short count is heap corruption, so the
    # declared numbers must equal the parser's own walk of the Segments.
    segments = points = 0
    for curve in reset["Curves"]:
        data = curve["Segments"]
        pos = 0
        while pos < len(data):
            if pos == 0:
                points += 1
                pos += 2
            if data[pos] == 1:  # bezier: 3 points, 7 floats
                points += 3
                pos += 7
            else:  # linear / stepped / inverse-stepped: 1 point, 3 floats
                points += 1
                pos += 3
            segments += 1
    assert reset["Meta"]["TotalSegmentCount"] == segments
    assert reset["Meta"]["TotalPointCount"] == points
    assert reset["Meta"]["CurveCount"] == len(reset["Curves"])


def test_schema_rejects_tags_the_avatar_cannot_perform() -> None:
    jsonschema.validate({"emotion": "wink", "motion": "wave"}, OUTPUT_SCHEMA)
    for unit in ({"emotion": "happy"}, {"motion": "nod"}):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(unit, OUTPUT_SCHEMA)
