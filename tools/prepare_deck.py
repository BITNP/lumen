#!/usr/bin/env python3
"""Prepare a slide deck for the presenter: PPT/PPTX/PDF -> per-page PNG + texts + manifest.

Run inside the repo environment (needs the presenter package for the shared
bounds, plus system tools soffice and pdftoppm; pdftotext is optional and only
needed for automatic page-summary extraction):

    uv run --locked python tools/prepare_deck.py slides.pptx [--name intro]
        [--out decks] [--dpi 120] [--first 1] [--last 20]
        [--scripts scripts.json] [--no-scripts] [--force]

Produces <out>/<name>/001.png … NNN.png plus manifest.json carrying two
per-page texts (ADR-0006/0007): the page summary (页面内容: what the slide
shows, extracted from the PDF text layer with pdftotext, or replaced by
hand/vision-model text via the "summary" key) and the suggested speaker
script (建议讲稿: how to teach the page, authored via the "script" key or a
plain string).  --scripts is a JSON list aligned with pages, or a {"1": …}
mapping of 1-based pages; each value is a string (script only) or a
{"summary": "…", "script": "…"} object.  Manifest schema_version: "1.2.0"
with summaries, "1.1.0" scripts-only, "1.0.0" without texts.  The Agent opens
the result with {"action":"open_deck","deck":"<name>"} and receives both texts
on every queue_finished.
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
        help="JSON file of per-page texts: a list aligned with pages or a "
        "{\"1\": …} page map; each value is a string (suggested script) or a "
        "{\"summary\": \"…\", \"script\": \"…\"} object (ADR-0007)",
    )
    parser.add_argument(
        "--no-scripts",
        action="store_true",
        help="skip pdftotext summary extraction and --scripts merging",
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
        summaries: list[str] = []
        scripts: list[str] = []
        if not args.no_scripts:
            summaries = extract_summaries(pdf, first=args.first, count=len(pages))
            if args.scripts is not None:
                summaries, scripts = apply_script_overrides(
                    summaries, args.scripts, count=len(pages)
                )
        if summaries or scripts:
            missing = sum(1 for text in summaries if not text)
            if missing:
                print(f"note: {missing} page(s) have no summary text; "
                      "consider --scripts for decks without a usable text layer")
        target.mkdir(parents=True)
        manifest_pages = []
        for index, page in enumerate(pages, start=1):
            page_name = f"{index:03d}.png"
            shutil.move(str(page), target / page_name)
            manifest_pages.append(page_name)
        manifest: dict[str, object] = {
            "schema_version": (
                "1.2.0" if any(summaries) else "1.1.0" if any(scripts) else "1.0.0"
            ),
            "title": source.stem,
            "total_pages": len(manifest_pages),
            "pages": manifest_pages,
        }
        if any(scripts):
            manifest["scripts"] = scripts
        if any(summaries):
            manifest["summaries"] = summaries
        rendered = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        if len(rendered.encode("utf-8")) > MAX_MANIFEST_BYTES:
            shutil.rmtree(target)
            return fail(
                f"manifest is {len(rendered.encode('utf-8'))} bytes, over the "
                f"{MAX_MANIFEST_BYTES} limit; shorten texts or export fewer pages"
            )
        (target / "manifest.json").write_text(rendered, encoding="utf-8")
    notes = []
    if any(summaries):
        notes.append(f"{sum(1 for s in summaries if s)} summary(ies)")
    if any(scripts):
        notes.append(f"{sum(1 for s in scripts if s)} script(s)")
    text_note = f", {(' + '.join(notes))}" if notes else ""
    print(f"deck {name}: {len(manifest_pages)} page(s){text_note} -> {target}")
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


def extract_summaries(pdf: Path, *, first: int, count: int) -> list[str]:
    """Per-page text layer via pdftotext (form-feed separated); [] if unavailable."""
    if shutil.which("pdftotext") is None:
        print("note: pdftotext not found; deck will have no summaries "
              "(install poppler-utils or pass --scripts)")
        return []
    result = subprocess.run(
        ["pdftotext", "-f", str(first), "-l", str(first + count - 1), str(pdf), "-"],
        check=False,
        capture_output=True,
        timeout=300,
    )
    if result.returncode != 0:
        print("note: pdftotext failed; deck will have no summaries (pass --scripts instead)")
        return []
    chunks = result.stdout.decode("utf-8", "replace").split("\f")
    summaries = [normalize_page_text(chunk) for chunk in chunks[:count]]
    return summaries + [""] * (count - len(summaries))  # shorter PDF still aligns


def apply_script_overrides(
    summaries: list[str], path: Path, *, count: int
) -> tuple[list[str], list[str]]:
    """Merge a --scripts JSON file into summaries; return (summaries, scripts).

    Each page value is a string (suggested script only) or a
    ``{"summary": …, "script": …}`` object (ADR-0007); absent keys keep the
    pdftotext summary or an empty script.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"--scripts file unreadable: {exc}")
    merged_summaries = list(summaries) + [""] * max(0, count - len(summaries))
    scripts = [""] * count

    def assign(page: int, value: object) -> None:
        if isinstance(value, str):
            scripts[page - 1] = normalize_page_text(value)
        elif isinstance(value, dict):
            unknown = set(value) - {"summary", "script"}
            if unknown:
                raise SystemExit(f"--scripts page {page} has unknown keys: {sorted(unknown)}")
            for key, target in (("summary", merged_summaries), ("script", scripts)):
                if key in value:
                    text = value[key]
                    if not isinstance(text, str):
                        raise SystemExit(f"--scripts page {page} {key} is not a string")
                    target[page - 1] = normalize_page_text(text)
        else:
            raise SystemExit(f"--scripts page {page} value is not a string or object")

    if isinstance(data, list):
        if len(data) != count:
            raise SystemExit(f"--scripts list has {len(data)} entries, deck has {count} pages")
        for index, value in enumerate(data, start=1):
            assign(index, value)
    elif isinstance(data, dict):
        for key, value in data.items():
            if not str(key).isdigit() or not 1 <= int(key) <= count:
                raise SystemExit(f"--scripts page key {key!r} outside 1..{count}")
            assign(int(key), value)
    else:
        raise SystemExit("--scripts file must be a JSON list or object")
    return merged_summaries[:count], scripts


def normalize_page_text(text: str) -> str:
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
