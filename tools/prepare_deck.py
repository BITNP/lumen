#!/usr/bin/env python3
"""Prepare a slide deck for the presenter: PPT/PPTX/PDF -> per-page PNG + script + manifest.

Run inside the repo environment (needs the presenter package for the shared
bounds, plus system tools soffice and pdftoppm; pdftotext is optional and only
needed for automatic script extraction):

    uv run --locked python tools/prepare_deck.py slides.pptx [--name intro]
        [--out decks] [--dpi 120] [--first 1] [--last 20]
        [--scripts scripts.json] [--no-scripts] [--force]

Produces <out>/<name>/001.png … NNN.png plus manifest.json: per-page speaker
scripts (讲稿, ADR-0006) are extracted from the PDF text layer with pdftotext
and can be replaced by hand- or vision-model-written text via --scripts (a
JSON list aligned with pages, or a {"1": "…"} mapping of 1-based pages).
Manifest schema_version is "1.1.0" with scripts, "1.0.0" without.  The Agent
opens the result with the ppt unit field {"action":"open_deck","deck":"<name>"}
and receives each page's script on queue_finished.
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
    MAX_MANIFEST_BYTES,
    MAX_PAGE_BYTES,
    MAX_PAGES,
    MAX_SCRIPT_CHARS,
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
    parser.add_argument("--first", type=int, default=1, help="first page to export (1-based)")
    parser.add_argument("--last", type=int, default=MAX_PAGES, help="last page to export")
    parser.add_argument(
        "--scripts",
        type=Path,
        help="JSON file of per-page speaker scripts overriding the extracted text "
        "(a list aligned with pages, or a {\"1\": \"…\"} page mapping)",
    )
    parser.add_argument(
        "--no-scripts", action="store_true", help="skip text extraction; write a 1.0.0 manifest"
    )
    parser.add_argument("--force", action="store_true", help="replace an existing deck")
    args = parser.parse_args()

    source: Path = args.input
    if not source.is_file():
        return fail(f"input not found: {source}")
    suffix = source.suffix.lower()
    if suffix not in OFFICE_SUFFIXES:
        return fail(f"unsupported input type {suffix!r}; want one of {sorted(OFFICE_SUFFIXES)}")
    if args.first < 1 or args.last < args.first:
        return fail("bad page range: need 1 <= --first <= --last")

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
        pages = render_pages(pdf, Path(tmp), dpi=args.dpi, first=args.first, last=args.last)
        validate_pages(pages)
        scripts: list[str] = []
        if not args.no_scripts:
            scripts = extract_scripts(pdf, first=args.first, count=len(pages))
        if args.scripts is not None:
            scripts = apply_script_overrides(scripts, args.scripts, count=len(pages))
        if any(scripts):
            empty = sum(1 for script in scripts if not script)
            if empty:
                print(f"note: {empty} page(s) have no script text; "
                      "consider --scripts for decks without a usable text layer")
        target.mkdir(parents=True)
        manifest_pages = []
        for index, page in enumerate(pages, start=1):
            page_name = f"{index:03d}.png"
            shutil.move(str(page), target / page_name)
            manifest_pages.append(page_name)
        manifest: dict[str, object] = {
            "schema_version": SCHEMA_VERSION if any(scripts) else "1.0.0",
            "title": source.stem,
            "total_pages": len(manifest_pages),
            "pages": manifest_pages,
        }
        if any(scripts):
            manifest["scripts"] = scripts
        rendered = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        if len(rendered.encode("utf-8")) > MAX_MANIFEST_BYTES:
            shutil.rmtree(target)
            return fail(
                f"manifest is {len(rendered.encode('utf-8'))} bytes, over the "
                f"{MAX_MANIFEST_BYTES} limit; shorten scripts or export fewer pages"
            )
        (target / "manifest.json").write_text(rendered, encoding="utf-8")
    script_note = f", {sum(1 for s in scripts if s)} script(s)" if any(scripts) else ""
    print(f"deck {name}: {len(manifest_pages)} page(s){script_note} -> {target}")
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


def render_pages(pdf: Path, workdir: Path, *, dpi: int, first: int, last: int) -> list[Path]:
    if shutil.which("pdftoppm") is None:
        raise SystemExit("pdftoppm (poppler-utils) is required to render pages")
    prefix = workdir / "page"
    subprocess.run(
        ["pdftoppm", "-png", "-r", str(dpi), "-f", str(first), "-l", str(last),
         str(pdf), str(prefix)],
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


def extract_scripts(pdf: Path, *, first: int, count: int) -> list[str]:
    """Per-page text layer via pdftotext (form-feed separated); [] if unavailable."""
    if shutil.which("pdftotext") is None:
        print("note: pdftotext not found; deck will have no scripts "
              "(install poppler-utils or pass --scripts)")
        return []
    result = subprocess.run(
        ["pdftotext", "-f", str(first), "-l", str(first + count - 1), str(pdf), "-"],
        check=False,
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        print("note: pdftotext failed; deck will have no scripts (pass --scripts instead)")
        return []
    chunks = result.stdout.decode("utf-8", "replace").split("\f")
    scripts = [normalize_script(chunk) for chunk in chunks[:count]]
    return scripts + [""] * (count - len(scripts))  # shorter PDF still aligns


def apply_script_overrides(scripts: list[str], path: Path, *, count: int) -> list[str]:
    """Merge a --scripts JSON file (list aligned with pages, or {page: text} map)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"--scripts file unreadable: {exc}")
    merged = list(scripts) + [""] * max(0, count - len(scripts))
    if isinstance(data, list):
        if len(data) != count:
            raise SystemExit(f"--scripts list has {len(data)} entries, deck has {count} pages")
        for index, value in enumerate(data):
            if not isinstance(value, str):
                raise SystemExit(f"--scripts entry {index + 1} is not a string")
            merged[index] = normalize_script(value)
    elif isinstance(data, dict):
        for key, value in data.items():
            if not str(key).isdigit() or not 1 <= int(key) <= count:
                raise SystemExit(f"--scripts page key {key!r} outside 1..{count}")
            if not isinstance(value, str):
                raise SystemExit(f"--scripts page {key} value is not a string")
            merged[int(key) - 1] = normalize_script(value)
    else:
        raise SystemExit("--scripts file must be a JSON list or object")
    return merged[:count]


def normalize_script(text: str) -> str:
    """Collapse whitespace, cap at MAX_SCRIPT_CHARS (manifest bound ADR-0006)."""
    collapsed = re.sub(r"[ \t\r\f\v]+", " ", text)
    collapsed = re.sub(r" ?\n ?", "\n", collapsed)
    collapsed = re.sub(r"\n{2,}", "\n", collapsed).strip()
    if len(collapsed) > MAX_SCRIPT_CHARS:
        collapsed = collapsed[: MAX_SCRIPT_CHARS - 1] + "…"
    return collapsed


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
