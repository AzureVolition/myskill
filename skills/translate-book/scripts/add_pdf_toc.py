#!/usr/bin/env python3
"""Add clickable bookmarks and contents-page links to a generated PDF.

The implementation uses pypdf's incremental writer, so existing page content
streams, images, fonts, and metadata are not recompressed.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.annotations import Link
    from pypdf.generic import Fit, NameObject
except ImportError:
    PdfReader = PdfWriter = Link = Fit = NameObject = None


class HeadingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headings: list[dict[str, object]] = []
        self._level: int | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        match = re.fullmatch(r"h([1-6])", tag.lower())
        if match and self._level is None:
            self._level = int(match.group(1))
            self._buffer = []

    def handle_data(self, data):
        if self._level is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag):
        match = re.fullmatch(r"h([1-6])", tag.lower())
        if not match or self._level is None:
            return
        title = " ".join(html.unescape("".join(self._buffer)).split())
        if title:
            self.headings.append({"level": self._level, "title": title})
        self._level = None
        self._buffer = []


@dataclass
class TextLine:
    y: float
    height: float
    parts: list[str] = field(default_factory=list)
    x_start: float = float("inf")
    x_end: float = 0.0

    @property
    def text(self) -> str:
        return "".join(self.parts)


def extract_headings(html_path: Path) -> list[dict[str, object]]:
    parser = HeadingParser()
    parser.feed(html_path.read_text(encoding="utf-8"))
    parser.close()
    return parser.headings


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\u00ad", "").replace("\u200b", "")
    return "".join(value.split())


def page_text(page) -> str:
    try:
        return page.extract_text() or ""
    except Exception:
        return ""


def page_has_heading(text: str, needle: str) -> bool:
    if not needle:
        return False
    normalized_lines = {normalized(line) for line in text.splitlines()}
    return needle in normalized_lines or needle in normalized(text)


def locate_heading_pages(
    headings: list[dict[str, object]],
    pages: list[str],
) -> tuple[int | None, dict[int, int]]:
    page_norms = [normalized(text) for text in pages]
    best_page: int | None = None
    best_matches = 0
    for index in range(min(len(pages), 12)):
        matches = sum(
            1
            for heading in headings
            if normalized(str(heading["title"])) in page_norms[index]
        )
        if matches > best_matches:
            best_page = index
            best_matches = matches
    if best_matches < 2:
        best_page = None

    toc_labels = {"目录", "目次", "contents"}
    toc_label_index = next(
        (
            index
            for index, heading in enumerate(headings)
            if normalized(str(heading["title"])).casefold() in toc_labels
        ),
        None,
    )

    locations: dict[int, int] = {}
    search_start = (best_page if best_page is not None else -1) + 1
    for index, heading in enumerate(headings):
        title = str(heading["title"]).strip()
        needle = normalized(title)
        if not needle:
            continue
        if index == toc_label_index and best_page is not None:
            locations[index] = best_page
            continue
        for candidate in range(search_start, len(pages)):
            if page_has_heading(pages[candidate], needle):
                locations[index] = candidate
                search_start = candidate
                break
    return best_page, locations


def extract_lines(page) -> list[TextLine]:
    lines: list[TextLine] = []

    def visitor(text, cm, tm, _font_dict, _font_size):
        if not text or not text.strip():
            return
        x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
        y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
        height = max(abs(cm[3] * tm[3]), abs(cm[0] * tm[0]), 1.0)
        match = next((line for line in lines if abs(line.y - y) <= 2.0), None)
        if match is None:
            match = TextLine(y=y, height=height)
            lines.append(match)
        match.parts.append(text)
        match.x_start = min(match.x_start, x)
        match.x_end = max(match.x_end, x + max(height, len(text) * height * 0.55))
        match.height = max(match.height, height)

    page.extract_text(visitor_text=visitor)
    return sorted(lines, key=lambda line: line.y)


def contents_line_title(line: TextLine) -> str:
    value = re.sub(r"[\s.…·]+\d+\s*$", "", line.text)
    return normalized(value)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_bookmarked_pdf(
    pdf_path: Path,
    headings: list[dict[str, object]],
    output_path: Path,
) -> dict[str, object]:
    reader = PdfReader(str(pdf_path))
    pages = [page_text(page) for page in reader.pages]
    toc_page, locations = locate_heading_pages(headings, pages)

    shutil.copy2(pdf_path, output_path)
    writer = PdfWriter(
        str(output_path),
        clone_from=str(output_path),
        incremental=True,
    )

    # Replace any prior generated outline rather than accumulating duplicate
    # bookmarks when an older build is post-processed again.
    if NameObject("/Outlines") in writer._root_object:
        del writer._root_object[NameObject("/Outlines")]

    hierarchy: list[tuple[int, object]] = []
    bookmark_count = 0
    last_bookmark: tuple[str, int] | None = None
    for index, heading in enumerate(headings):
        page_index = locations.get(index)
        if page_index is None:
            continue
        title = str(heading["title"]).strip()
        key = normalized(title)
        if not key:
            continue
        if last_bookmark == (key, page_index):
            continue
        level = int(heading["level"])
        while hierarchy and hierarchy[-1][0] >= level:
            hierarchy.pop()
        parent = hierarchy[-1][1] if hierarchy else None
        page_height = float(reader.pages[page_index].mediabox.height)
        node = writer.add_outline_item(
            title,
            page_index,
            parent=parent,
            fit=Fit.fit_horizontally(top=page_height),
        )
        hierarchy.append((level, node))
        bookmark_count += 1
        last_bookmark = (key, page_index)

    visible_link_count = 0
    if toc_page is not None:
        toc_page_number = toc_page + 1
        toc_title = next(
            (
                str(item["title"])
                for item in headings
                if normalized(str(item["title"])).casefold() in {"目录", "目次", "contents"}
            ),
            "目录",
        )
        source_items = {
            normalized(str(heading["title"])): index
            for index, heading in enumerate(headings)
            if index in locations
        }
        linked: set[str] = set()
        page_width = float(reader.pages[toc_page].mediabox.width)
        for line in extract_lines(reader.pages[toc_page]):
            title_key = contents_line_title(line)
            if (
                not title_key
                or title_key == normalized(toc_title)
                or title_key in linked
                or title_key not in source_items
            ):
                continue
            target_index = locations[source_items[title_key]]
            target_height = float(reader.pages[target_index].mediabox.height)
            annotation = Link(
                rect=(
                    max(0.0, line.x_start - 3),
                    line.y - line.height,
                    min(page_width, max(line.x_end, page_width - 72)),
                    line.y + 2,
                ),
                target_page_index=target_index,
                fit=Fit.fit_horizontally(top=target_height),
                border=[0, 0, 0],
            )
            writer.add_annotation(toc_page, annotation)
            linked.add(title_key)
            visible_link_count += 1

    writer.write(str(output_path))
    return {
        "bookmarks": bookmark_count,
        "visible_toc_links": visible_link_count,
        "toc_page": toc_page_number if toc_page is not None else None,
        "pages": len(reader.pages),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add clickable PDF outline and contents-page links"
    )
    parser.add_argument("--html", required=True, type=Path)
    parser.add_argument("--pdf", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if PdfReader is None:
        print(
            "PDF TOC warning: pypdf is required. "
            "Install it with: python3 -m pip install --user 'pypdf>=5,<7'",
            file=sys.stderr,
        )
        return 2

    html_path = args.html.resolve()
    pdf_path = args.pdf.resolve()
    if not html_path.is_file() or not pdf_path.is_file():
        print("PDF TOC: input HTML or PDF is missing", file=sys.stderr)
        return 1

    report_path = pdf_path.with_suffix(pdf_path.suffix + ".toc.json")
    input_hash = sha256(pdf_path)
    if not args.force and report_path.is_file():
        try:
            previous = json.loads(report_path.read_text(encoding="utf-8"))
            if (
                previous.get("status") == "ok"
                and previous.get("pdf_sha256") == input_hash
                and int(previous.get("bookmarks", 0)) > 0
            ):
                print(
                    "PDF TOC already present "
                    f"({previous['bookmarks']} bookmarks, "
                    f"{previous.get('visible_toc_links', 0)} contents links)"
                )
                return 0
        except (OSError, ValueError, TypeError):
            pass

    headings = extract_headings(html_path)
    if not headings:
        print("PDF TOC: no headings found in HTML", file=sys.stderr)
        return 1

    original_size = pdf_path.stat().st_size
    with tempfile.TemporaryDirectory(
        prefix="translate-book-toc-",
        dir=str(pdf_path.parent),
    ) as temp_name:
        output_path = Path(temp_name) / pdf_path.name
        try:
            details = build_bookmarked_pdf(pdf_path, headings, output_path)
        except Exception as exc:
            print(f"PDF TOC warning: {exc}", file=sys.stderr)
            return 2

        if not output_path.is_file() or output_path.stat().st_size == 0:
            print("PDF TOC: writer did not create an output PDF", file=sys.stderr)
            return 1
        os.replace(output_path, pdf_path)

    report = {
        "status": "ok",
        "pdf_sha256": sha256(pdf_path),
        "heading_candidates": len(headings),
        "original_bytes": original_size,
        "output_bytes": pdf_path.stat().st_size,
        **details,
    }
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "PDF TOC added "
        f"({report.get('bookmarks', 0)} bookmarks, "
        f"{report.get('visible_toc_links', 0)} contents links, "
        f"+{report.get('output_bytes', 0) - report.get('original_bytes', 0):,} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
