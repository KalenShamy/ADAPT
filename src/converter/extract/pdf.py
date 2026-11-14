"""Read a v010 protocol PDF into the shared Document model."""

import re
from pathlib import Path

import pymupdf

from pdf_to_cpt_docx import (
    Document, body_font_size, classify_paragraphs, collapse_redactions, join_across_pages,
    merge_split_tables, read_pdf, structure,
)

BOOKMARK_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)(?:\.?\s|$)")


def numbered_bookmarks(path: Path) -> list[tuple[str, str, int]]:
    """(number, title, page) for each PDF bookmark that starts with a section number."""
    with pymupdf.open(path) as pdf:
        bookmarks = pdf.get_toc()
    rows = []
    for _, text, page in bookmarks:
        match = BOOKMARK_NUMBER.match(text)
        if match:
            rows.append((match.group(1), text[match.end():].strip(), page))
    return rows


def load_pdf(path: Path) -> Document:
    items, toc_pages = read_pdf(str(path))
    document = structure(items, toc_pages, body_font_size(items), numbered_bookmarks(path))
    for part in ("front", "body"):
        blocks = collapse_redactions(merge_split_tables(join_across_pages(getattr(document, part))))
        classify_paragraphs(blocks)
        setattr(document, part, blocks)
    return document
