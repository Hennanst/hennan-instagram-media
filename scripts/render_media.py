#!/usr/bin/env python3
"""Deterministic renderer for Instagram staging media.

Public repository contains only publication-intended assets. The script renders
HTML sources to 1080x1350 JPEGs, verifies the exact NeuroEvidence mark, and
writes byte-level manifests for G9 pre-publish QA.
"""
from __future__ import annotations

import base64
import hashlib
import json
import tempfile
from html.parser import HTMLParser
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image
from weasyprint import HTML

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "sources"
FEED = ROOT / "feed"
EXPECTED_WIDTH = 1080
EXPECTED_HEIGHT = 1350
RENDER_DPI = 96
JPEG_QUALITY = 94
CANONICAL_NEUROEVIDENCE_SHA256 = "488da8579d006b92c130de0000bf9c26da8b7cdd640a8046066dfb3f7f234a00"


class SourceAuditParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.pages = 0
        self.mark_hashes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {k: v for k, v in attrs}
        if data.get("data-document-role") == "page":
            self.pages += 1
        if tag.lower() != "img":
            return
        classes = set((data.get("class") or "").split())
        if "neuro-mark" not in classes:
            return
        src = data.get("src") or ""
        prefix = "data:image/png;base64,"
        if not src.startswith(prefix):
            raise RuntimeError("NeuroEvidence mark must be embedded as PNG data URI")
        raw = base64.b64decode(src[len(prefix):], validate=True)
        self.mark_hashes.append(hashlib.sha256(raw).hexdigest())


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def render_source(source: Path) -> None:
    job_id = source.stem
    html_text = source.read_text(encoding="utf-8")

    parser = SourceAuditParser()
    parser.feed(html_text)
    if parser.pages < 1:
        raise RuntimeError(f"{job_id}: no data-document-role=page sections found")
    if len(parser.mark_hashes) != parser.pages:
        raise RuntimeError(
            f"{job_id}: NeuroEvidence mark count {len(parser.mark_hashes)} != page count {parser.pages}"
        )
    wrong = [h for h in parser.mark_hashes if h != CANONICAL_NEUROEVIDENCE_SHA256]
    if wrong:
        raise RuntimeError(f"{job_id}: non-canonical NeuroEvidence mark detected")

    out_dir = FEED / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("*.jpg"):
        stale.unlink()

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / f"{job_id}.pdf"
        HTML(filename=str(source)).write_pdf(str(pdf_path))
        doc = fitz.open(pdf_path)
        if doc.page_count != parser.pages:
            raise RuntimeError(
                f"{job_id}: rendered PDF page count {doc.page_count} != source page count {parser.pages}"
            )

        items = []
        scale = RENDER_DPI / 72.0
        matrix = fitz.Matrix(scale, scale)
        for index, page in enumerate(doc, start=1):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            if image.size != (EXPECTED_WIDTH, EXPECTED_HEIGHT):
                raise RuntimeError(
                    f"{job_id} page {index}: got {image.size}, expected {(EXPECTED_WIDTH, EXPECTED_HEIGHT)}"
                )
            output = out_dir / f"{index:02d}.jpg"
            image.save(
                output,
                format="JPEG",
                quality=JPEG_QUALITY,
                optimize=True,
                progressive=False,
                subsampling=0,
            )
            items.append(
                {
                    "page": index,
                    "file": output.name,
                    "sha256": sha256_file(output),
                    "bytes": output.stat().st_size,
                    "width": EXPECTED_WIDTH,
                    "height": EXPECTED_HEIGHT,
                    "mode": "RGB",
                }
            )

    manifest = {
        "job_id": job_id,
        "source": source.name,
        "source_sha256": sha256_file(source),
        "pages": parser.pages,
        "format": "JPEG",
        "dimensions": [EXPECTED_WIDTH, EXPECTED_HEIGHT],
        "render_dpi": RENDER_DPI,
        "jpeg_quality": JPEG_QUALITY,
        "neuroevidence_mark": {
            "status": "PASS_ALL_PAGES",
            "sha256": CANONICAL_NEUROEVIDENCE_SHA256,
            "count": len(parser.mark_hashes),
        },
        "items": items,
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"PASS {job_id}: {parser.pages} page(s), canonical mark on every page")


def main() -> None:
    sources = sorted(SOURCES.glob("HST-IG-*.html"))
    if not sources:
        print("No sources found; nothing to render.")
        return
    FEED.mkdir(parents=True, exist_ok=True)
    for source in sources:
        render_source(source)


if __name__ == "__main__":
    main()
