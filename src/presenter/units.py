"""Output unit parsing: the JSON payload of an output_unit frame -> ExpressionUnit.

The hub already validated the payload against OUTPUT_SCHEMA before dispatch, so
parsing here is defensive: a unit that cannot be understood is rejected with a
reason that goes straight into the error output result (ADR-0002).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

PptAction = Literal["open_deck", "goto", "next", "prev", "close"]
_PPT_ACTIONS = ("open_deck", "goto", "next", "prev", "close")


class UnitError(ValueError):
    """Raised when an output unit payload cannot be executed."""


@dataclass(frozen=True, slots=True)
class PptCommand:
    """Deck instruction riding on a unit (ADR-0003: list order = performance order)."""

    action: PptAction
    deck: str | None = None
    page: int | None = None


@dataclass(frozen=True, slots=True)
class ExpressionUnit:
    """One executable expression unit (waycast OutputUnit payload, ADR-0003)."""

    speech: str | None = None
    subtitle: str | None = None
    emotion: str | None = None
    motion: str | None = None
    control: str | None = None
    ppt: PptCommand | None = None

    @property
    def is_stop(self) -> bool:
        return self.control == "stop"

    @property
    def subtitle_text(self) -> str:
        return self.subtitle or self.speech or ""

    def visual_tags(self) -> dict[str, str]:
        tags: dict[str, str] = {}
        if self.emotion:
            tags["emotion"] = self.emotion
        if self.motion:
            tags["motion"] = self.motion
        return tags


def parse_unit(payload: dict[str, Any]) -> ExpressionUnit:
    """Build an ExpressionUnit from a dispatched payload; raise UnitError if empty."""
    unit = ExpressionUnit(
        speech=_text(payload.get("speech")),
        subtitle=_text(payload.get("subtitle")),
        emotion=_text(payload.get("emotion")),
        motion=_text(payload.get("motion")),
        control=_text(payload.get("control")),
        ppt=_parse_ppt(payload.get("ppt")),
    )
    if unit.is_stop:
        return unit
    if unit.control is not None:
        raise UnitError(f"unknown control value: {unit.control!r}")
    if not (unit.speech or unit.emotion or unit.motion or unit.ppt):
        raise UnitError("unit carries no executable field")
    return unit


def _parse_ppt(value: Any) -> PptCommand | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise UnitError("ppt must be an object")
    ppt = cast("dict[str, Any]", value)
    action = ppt.get("action")
    if action not in _PPT_ACTIONS:
        raise UnitError(f"unknown ppt action: {action!r}")
    deck = _text(ppt.get("deck"))
    page = ppt.get("page")
    if page is not None and (
        not isinstance(page, int) or isinstance(page, bool) or page < 1
    ):
        raise UnitError("ppt page must be a positive integer")
    if action == "open_deck" and deck is None:
        raise UnitError("ppt open_deck requires a deck name")
    if action == "goto" and page is None:
        raise UnitError("ppt goto requires a page")
    return PptCommand(action=action, deck=deck, page=page)


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


__all__ = ["ExpressionUnit", "PptCommand", "PptAction", "UnitError", "parse_unit"]
