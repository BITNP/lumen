"""Deck state machine: manifest validation, page flow, failure semantics."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from placeholders import make_png, write_deck
from presenter.decks import DeckError, DeckManager
from presenter.units import PptCommand, parse_unit


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return write_deck(tmp_path, "demo", pages=3)


def _ppt(action: str, **fields: object) -> PptCommand:
    unit = parse_unit({"ppt": {"action": action, **fields}})
    assert unit.ppt is not None
    return unit.ppt


def test_open_then_next_goto_prev_close(root: Path) -> None:
    manager = DeckManager(root)
    show = manager.apply(_ppt("open_deck", deck="demo"))
    assert show == {
        "type": "ppt",
        "action": "show",
        "image": str(root / "demo" / "001.png"),
        "deck": "demo",
        "page": 1,
        "total_pages": 3,
    }
    assert manager.snapshot().page == 1
    assert manager.apply(_ppt("next"))["page"] == 2
    assert manager.apply(_ppt("goto", page=3))["page"] == 3
    assert manager.apply(_ppt("prev"))["page"] == 2
    snap = manager.snapshot()
    assert (snap.deck, snap.page, snap.total_pages) == ("demo", 2, 3)
    assert manager.apply(_ppt("close")) == {"type": "ppt", "action": "hide"}
    assert manager.snapshot().deck is None


def test_edges_report_rather_than_wrap(root: Path) -> None:
    manager = DeckManager(root)
    manager.apply(_ppt("open_deck", deck="demo"))
    with pytest.raises(DeckError, match="first page"):
        manager.apply(_ppt("prev"))
    manager.apply(_ppt("goto", page=3))
    with pytest.raises(DeckError, match="last page"):
        manager.apply(_ppt("next"))
    with pytest.raises(DeckError, match="out of range"):
        manager.apply(_ppt("goto", page=4))
    assert manager.snapshot().page == 3, "failed commands must not change state"


def test_commands_without_open_deck_fail(root: Path) -> None:
    manager = DeckManager(root)
    for call in (_ppt("next"), _ppt("goto", page=1), _ppt("prev")):
        with pytest.raises(DeckError, match="no deck"):
            manager.apply(call)
    assert manager.apply(_ppt("close")) == {"type": "ppt", "action": "hide"}


def test_unknown_or_unsafe_deck(root: Path) -> None:
    manager = DeckManager(root)
    with pytest.raises(DeckError, match="unsafe"):
        manager.apply(_ppt("open_deck", deck="../evil"))
    with pytest.raises(DeckError, match="manifest unavailable"):
        manager.apply(_ppt("open_deck", deck="ghost"))


def test_manifest_rejections(tmp_path: Path) -> None:
    def manifest_case(
        pages_value: object,
        version: str = "1.0.0",
        scripts_value: object = None,
        summaries_value: object = None,
    ) -> Path:
        directory = tmp_path / "case"
        directory.mkdir(exist_ok=True)
        manifest: dict[str, object] = {"schema_version": version, "pages": pages_value}
        if scripts_value is not None:
            manifest["scripts"] = scripts_value
        if summaries_value is not None:
            manifest["summaries"] = summaries_value
        (directory / "manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        return tmp_path

    manager = DeckManager(manifest_case(["001.png"], version="2.0.0"))
    with pytest.raises(DeckError, match="schema_version"):
        manager.apply(_ppt("open_deck", deck="case"))
    manager = DeckManager(manifest_case(["page1.png"]))
    with pytest.raises(DeckError, match="pages list"):
        manager.apply(_ppt("open_deck", deck="case"))
    manager = DeckManager(manifest_case([f"{i:03d}.png" for i in range(1, 202)]))
    with pytest.raises(DeckError, match="pages list"):
        manager.apply(_ppt("open_deck", deck="case"))
    manager = DeckManager(manifest_case(["001.png"], version="1.1.0", scripts_value=["a", "b"]))
    with pytest.raises(DeckError, match="scripts list"):
        manager.apply(_ppt("open_deck", deck="case"))
    manager = DeckManager(manifest_case(["001.png"], version="1.1.0", scripts_value=[42]))
    with pytest.raises(DeckError, match="scripts list"):
        manager.apply(_ppt("open_deck", deck="case"))
    manager = DeckManager(
        manifest_case(["001.png"], version="1.1.0", scripts_value=["讲稿" * 2001])
    )
    with pytest.raises(DeckError, match="scripts list"):
        manager.apply(_ppt("open_deck", deck="case"))
    manager = DeckManager(
        manifest_case(["001.png"], version="1.2.0", summaries_value=["a", "b"])
    )
    with pytest.raises(DeckError, match="summaries list"):
        manager.apply(_ppt("open_deck", deck="case"))
    manager = DeckManager(
        manifest_case(["001.png"], version="1.2.0", summaries_value=[42])
    )
    with pytest.raises(DeckError, match="summaries list"):
        manager.apply(_ppt("open_deck", deck="case"))


def test_scripts_and_summaries_follow_the_shown_page(tmp_path: Path) -> None:
    """ADR-0006/0007: the snapshot carries the current page's script and summary."""
    root = write_deck(
        tmp_path,
        "scripted",
        pages=3,
        scripts=["第一页讲稿", "  ", "第三页讲稿"],
        summaries=["第一页内容", "第二页内容", ""],
    )
    manager = DeckManager(root)
    manager.apply(_ppt("open_deck", deck="scripted"))
    assert (manager.snapshot().script, manager.snapshot().summary) == (
        "第一页讲稿",
        "第一页内容",
    )
    manager.apply(_ppt("next"))
    snapshot = manager.snapshot()
    assert snapshot.script is None, "blank scripts surface as None"
    assert snapshot.summary == "第二页内容"
    manager.apply(_ppt("next"))
    snapshot = manager.snapshot()
    assert (snapshot.script, snapshot.summary) == ("第三页讲稿", None)
    manager.apply(_ppt("close"))
    assert (manager.snapshot().script, manager.snapshot().summary) == (None, None)


def test_v110_deck_without_summaries_still_reads(tmp_path: Path) -> None:
    """1.1.0（只有讲稿）继续可读；summary 为空。"""
    root = write_deck(tmp_path, "old", pages=2, scripts=["甲", "乙"])
    manager = DeckManager(root)
    manager.apply(_ppt("open_deck", deck="old"))
    assert manager.snapshot().script == "甲"
    assert manager.snapshot().summary is None


def test_page_file_must_be_a_bounded_png(tmp_path: Path) -> None:
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "manifest.json").write_text(
        json.dumps({"schema_version": "1.0.0", "pages": ["001.png"]}), encoding="utf-8"
    )
    (directory / "001.png").write_bytes(b"not a png at all, just text padding")
    manager = DeckManager(tmp_path)
    with pytest.raises(DeckError, match="not a PNG"):
        manager.apply(_ppt("open_deck", deck="bad"))
    (directory / "001.png").unlink()
    with pytest.raises(DeckError, match="unavailable"):
        manager.apply(_ppt("open_deck", deck="bad"))
    # A real PNG whose header claims oversized dimensions is rejected.
    oversized = make_png(2, 2)
    header = bytearray(oversized)
    header[16:20] = (9000).to_bytes(4, "big")
    (directory / "001.png").write_bytes(bytes(header))
    with pytest.raises(DeckError, match="dimensions"):
        manager.apply(_ppt("open_deck", deck="bad"))
