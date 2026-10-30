"""Deck state machine: pre-exported PNG page decks for the ppt unit field.

A deck is a directory under PRESENTER_DECKS_DIR prepared by
tools/prepare_deck.py: manifest.json (schema_version "1.0.0") plus per-page
PNG files.  The manager validates strictly (bounds mirror the old frontend's
hardened reader), applies ppt commands from expression units, keeps the
current deck/page state for status queries, and returns the bridge command
that shows or hides a slide.  Every failure raises DeckError; the caller
reports it as an async error result without breaking the speech pipeline.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from presenter.units import PptCommand

LOGGER = logging.getLogger(__name__)

MAX_PAGES = 200
MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_DIMENSION = 4096
SCHEMA_VERSION = "1.0.0"

SAFE_DECK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PAGE_NAME = re.compile(r"^[0-9]{1,3}\.png$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class DeckError(RuntimeError):
    """Raised when a ppt command cannot be executed; text goes into results."""


@dataclass(frozen=True, slots=True)
class DeckSnapshot:
    deck: str | None
    page: int | None
    total_pages: int | None


class DeckManager:
    """Host-side owner of the currently displayed deck (no Godot knowledge)."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._deck: str | None = None
        self._pages: list[str] = []
        self._page = 0

    def snapshot(self) -> DeckSnapshot:
        if self._deck is None:
            return DeckSnapshot(None, None, None)
        return DeckSnapshot(self._deck, self._page, len(self._pages))

    def apply(self, command: PptCommand) -> dict[str, Any]:
        """Apply one ppt command; return the bridge command to execute it."""
        if command.action == "close":
            self._deck, self._pages, self._page = None, [], 0
            return {"type": "ppt", "action": "hide"}
        if command.action == "open_deck":
            return self._open(command.deck or "")
        # goto / next / prev act on the open deck.
        if self._deck is None:
            raise DeckError("no deck is open; send ppt open_deck first")
        if command.action == "goto":
            target = command.page
            if target is None:  # schema enforces this; defensive
                raise DeckError("goto requires a page")
            if not 1 <= target <= len(self._pages):
                raise DeckError(f"page {target} out of range 1..{len(self._pages)}")
        elif command.action == "next":
            if self._page >= len(self._pages):
                raise DeckError(f"already on last page ({self._page}/{len(self._pages)})")
            target = self._page + 1
        else:  # prev
            if self._page <= 1:
                raise DeckError(f"already on first page ({self._page}/{len(self._pages)})")
            target = self._page - 1
        page_path = self._validated_page(self._deck, self._pages[target - 1])
        self._page = target  # commit only after the page file checks out
        return self._show(self._deck, target, page_path)

    # -- internals ---------------------------------------------------------

    def _open(self, deck: str) -> dict[str, Any]:
        if not SAFE_DECK_ID.match(deck):
            raise DeckError(f"unsafe deck name: {deck!r}")
        directory = self._root / deck
        pages = _read_manifest(directory / "manifest.json")
        page_path = self._validated_page(deck, pages[0])
        self._deck, self._pages, self._page = deck, pages, 1
        LOGGER.info("deck %s opened (%d pages)", deck, len(pages))
        return self._show(deck, 1, page_path)

    def _validated_page(self, deck: str, name: str) -> Path:
        path = self._root / deck / name
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise DeckError(f"page {name} unavailable: {exc.strerror or exc}") from exc
        if not 24 <= size <= MAX_PAGE_BYTES:
            raise DeckError(f"page {name} has invalid size {size}")
        with path.open("rb") as handle:
            header = handle.read(24)
        if header[:8] != _PNG_SIGNATURE:
            raise DeckError(f"page {name} is not a PNG")
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        if not 1 <= width <= MAX_DIMENSION or not 1 <= height <= MAX_DIMENSION:
            raise DeckError(f"page {name} has bad dimensions {width}x{height}")
        return path

    def _show(self, deck: str, page: int, path: Path) -> dict[str, Any]:
        return {
            "type": "ppt",
            "action": "show",
            "image": str(path),
            "deck": deck,
            "page": page,
            "total_pages": len(self._pages),
        }


def _read_manifest(path: Path) -> list[str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DeckError(f"deck manifest unavailable: {exc.strerror or exc}") from exc
    if len(raw) > MAX_MANIFEST_BYTES:
        raise DeckError("deck manifest too large")
    try:
        parsed: object = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise DeckError(f"deck manifest is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise DeckError("deck manifest is not an object")
    manifest = cast("dict[str, Any]", parsed)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise DeckError("unsupported deck manifest schema_version")
    raw_pages = manifest.get("pages")
    if not isinstance(raw_pages, list):
        raise DeckError("deck manifest pages list is invalid")
    page_entries = cast("list[object]", raw_pages)
    if not 1 <= len(page_entries) <= MAX_PAGES:
        raise DeckError("deck manifest pages list is invalid")
    pages: list[str] = []
    for item in page_entries:
        if not isinstance(item, str) or not _PAGE_NAME.match(item):
            raise DeckError("deck manifest pages list is invalid")
        pages.append(item)
    if len(set(pages)) != len(pages):
        raise DeckError("deck manifest pages list is invalid")
    return pages


__all__ = [
    "DeckManager",
    "DeckError",
    "DeckSnapshot",
    "SAFE_DECK_ID",
    "SCHEMA_VERSION",
    "MAX_PAGES",
    "MAX_PAGE_BYTES",
    "MAX_DIMENSION",
]
