"""Wire frame contract mirrors the waycast envelope (ADR-0008)."""
from __future__ import annotations

import pytest

from presenter.wire import PROTOCOL_VERSION, WireError, dumps, make_frame, parse_frame


def test_make_frame_roundtrip() -> None:
    frame = make_frame("result", "presenter", {"module": "presenter", "status": "ok"}, request_id="r1")
    assert frame["protocol_version"] == PROTOCOL_VERSION
    assert frame["type"] == "result"
    assert frame["request_id"] == "r1"
    parsed = parse_frame(dumps(frame))
    assert parsed == frame


def test_rejects_bad_version_and_shapes() -> None:
    with pytest.raises(WireError):
        parse_frame({"protocol_version": 99, "type": "input", "module": "m"})
    with pytest.raises(WireError):
        parse_frame(b"not json")
    with pytest.raises(WireError):
        parse_frame('{"protocol_version": 1, "module": "m"}')
    with pytest.raises(WireError):
        parse_frame({"protocol_version": 1, "type": "input", "module": "m", "payload": []})


def test_payload_defaults_to_dict() -> None:
    parsed = parse_frame({"protocol_version": 1, "type": "input", "module": "m"})
    assert parsed["payload"] == {}
