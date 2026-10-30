#!/usr/bin/env python3
"""Prepare a slide deck for the presenter: PPT/PPTX/PDF -> per-page PNG + manifest.

Run inside the repo environment (needs the presenter package for the shared
bounds, plus system tools soffice and pdftoppm):

    uv run --locked python tools/prepare_deck.py slides.pptx [--name intro]
        [--out decks] [--dpi 120] [--force]

Produces <out>/<name>/001.png … NNN.png plus manifest.json
(schema_version "1.0.0"), the format presenter.decks.DeckManager validates and
the Agent opens with the ppt unit field {"action":"open_deck","deck":"<name>"}.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from presenter.decks import (
    MAX_DIMENSION,
    MAX_PAGE_BYTES,
    MAX_PAGES,
    SAFE_DECK_ID,
    SCHEMA_VERSION,
)

OFFICE_SUFFIXES = {".pptx", ".ppt", ".odp", ".pdf"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("input", type=Path, help="PPT/PPTX/ODP/PDF file to export")
    parser.add_argument("--name", help="deck name (default: sanitized file stem)")
    parser.add_argument("--out", type=Path, default=Path("decks"), help="decks root dir")
    parser.add_argument("--dpi", type=int, default=120, help="render resolution")
    parser.add_argument("--force", action="store_true", help="replace an existing deck")
    args = parser.parse_args()

    source: Path = args.input
    if not source.is_file():
        return fail(f"input not found: {source}")
    suffix = source.suffix.lower()
    if suffix not in OFFICE_SUFFIXES:
        return fail(f"unsupported input type {suffix!r}; want one of {sorted(OFFICE_SUFFIXES)}")

    name = args.name or sanitize_name(source.stem)
    if not SAFE_DECK_ID.match(name):
        return fail(f"deck name {name!r} does not match {SAFE_DECK_ID.pattern}")
    target = args.out / name
    if target.exists():
        if not args.force:
            return fail(f"deck {target} exists; pass --force to replace it")
        shutil.rmtree(target)

    with tempfile.TemporaryDirectory(prefix="prepare-deck-") as tmp:
        pdf = to_pdf(source, Path(tmp)) if suffix != ".pdf" else source
        pages = render_pages(pdf, Path(tmp), dpi=args.dpi)
        validate_pages(pages)
        target.mkdir(parents=True)
        manifest_pages = []
        for index, page in enumerate(pages, start=1):
            page_name = f"{index:03d}.png"
            shutil.move(str(page), target / page_name)
            manifest_pages.append(page_name)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "title": source.stem,
            "total_pages": len(manifest_pages),
            "pages": manifest_pages,
        }
        (target / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(f"deck {name}: {len(manifest_pages)} page(s) -> {target}")
    return 0


def sanitize_name(stem: str) -> str:
    """ASCII-safe deck name derived from a file stem (non-ASCII drops out)."""
    ascii_text = stem.lower().encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9_-]+", "-", ascii_text).strip("-_")[:64].strip("-_")
    if not re.match(r"^[a-z0-9]", slug):
        slug = f"deck-{slug}" if slug else "deck"
    return slug[:64].strip("-_") or "deck"


def to_pdf(source: Path, workdir: Path) -> Path:
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice is None:
        raise SystemExit("soffice (LibreOffice) is required to convert office documents")
    subprocess.run(
        [soffice, "--headless", "--convert-to", "pdf", "--outdir", str(workdir), str(source)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=300,
    )
    pdf = workdir / (source.stem + ".pdf")
    if not pdf.is_file():
        raise SystemExit(f"soffice produced no PDF for {source}")
    return pdf


def render_pages(pdf: Path, workdir: Path, *, dpi: int) -> list[Path]:
    if shutil.which("pdftoppm") is None:
        raise SystemExit("pdftoppm (poppler-utils) is required to render pages")
    prefix = workdir / "page"
    subprocess.run(
        ["pdftoppm", "-png", "-r", str(dpi), "-f", "1", "-l", str(MAX_PAGES), str(pdf), str(prefix)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=600,
    )
    numbered = []
    for path in workdir.glob("page-*.png"):
        digits = re.search(r"page-(\d+)\.png$", path.name)
        if digits:
            numbered.append((int(digits.group(1)), path))
    pages = [path for _, path in sorted(numbered)]
    if not pages:
        raise SystemExit(f"pdftoppm rendered no pages from {pdf}")
    if len(pages) > MAX_PAGES:
        raise SystemExit(f"deck has more than {MAX_PAGES} pages")
    return pages


def validate_pages(pages: list[Path]) -> None:
    for page in pages:
        size = page.stat().st_size
        if size > MAX_PAGE_BYTES:
            raise SystemExit(f"{page.name} is {size} bytes, over the {MAX_PAGE_BYTES} limit")
        header = page.read_bytes()[:24]
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        if width > MAX_DIMENSION or height > MAX_DIMENSION:
            raise SystemExit(f"{page.name} is {width}x{height}, over {MAX_DIMENSION}px; lower --dpi")


def fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
