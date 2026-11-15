"""Document blocks, style names and the constants shared across the converter."""

import copy
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone


type JsonObject = dict[str, object]

TWIPS_PER_PT = 20
BOLD_FLAG = 16
ITALIC_FLAG = 2
SUPERSCRIPT_FLAG = 1
TOC_LEADER = re.compile(r"\.{5,}\s*\d*\s*$|\.{5,}")
REDACTION_MARK = "CCI"  # label sponsors print on redacted (commercially confidential) blocks
DEFAULT_CELL_PADDING = (5.0, 2.0)  # pt (horizontal, vertical) when a table has no text to measure
DEFAULT_TABLE_FONT_SIZE = 10.0  # pt, for a table with no measurable text
MAX_CAPTION_LENGTH = 220
MISSING_HEADING_TITLE = "[Heading not present in source PDF]"
REDACTED_HEADING_TITLE = "[REDACTED]"  # a heading whose number is printed but whose title is a black bar
REDACTED_TITLES = {REDACTION_MARK, REDACTED_HEADING_TITLE}

STYLE_NAMES = {
    "v011": {
        "headings": ["01Heading 1", "02Heading 2", "03Heading 3", "04Heading 4", "05Heading 5", "06Heading 6"],
        "front_heading": "08Subheading Bold",
        "body": "Normal", "bullet": "CPT_List Bullet", "caption": "Caption",
        "table_text": "Table Text", "field_label": "Heading (No TOC)", "page_title": "Heading 1 (No TOC)",
    },
}


@dataclass
class Span:
    text: str
    bold: bool
    italic: bool
    superscript: bool
    size: float


def space_span():
    return Span(" ", False, False, False, 0)


def spans_text(spans):
    return "".join(span.text for span in spans)


@dataclass
class Row:
    """One visual text row: PDF lines that share a baseline, left to right."""
    page: int
    x0: float
    y0: float
    y1: float
    block: int
    pieces: list  # list of (x0, list[Span])

    @property
    def text(self):
        return " ".join(spans_text(spans).strip() for _, spans in self.pieces).strip()

    @property
    def height(self):
        return self.y1 - self.y0

    def spans(self, skip_pieces=0):
        out = []
        for i, (_, spans) in enumerate(self.pieces[skip_pieces:]):
            if i:
                out.append(space_span())
            out.extend(spans)
        return out

    def first_piece_text(self):
        return spans_text(self.pieces[0][1]).strip()

    def visible_spans(self):
        return [span for _, spans in self.pieces for span in spans if span.text.strip()]

    def is_bold(self):
        visible = self.visible_spans()
        return bool(visible) and all(span.bold for span in visible)

    def starts_bold(self):
        visible = self.visible_spans()
        return bool(visible) and visible[0].bold

    def is_italic(self):
        visible = self.visible_spans()
        return bool(visible) and all(span.italic for span in visible)

    def size(self):
        sizes = [span.size for span in self.visible_spans()]
        return max(sizes) if sizes else 0


@dataclass
class Heading:
    number: str
    title: str
    page: int

    @property
    def level(self):
        return self.number.count(".") + 1


@dataclass
class Paragraph:
    spans: list
    kind: str = "body"  # body | bullet | caption | label
    level: int = 0
    page: int = 0
    first_x: float = 0
    align: str = "left"  # left | center | right

    @property
    def text(self):
        return spans_text(self.spans).strip()


@dataclass
class Table:
    grid: list  # rows of cells; a cell is None (covered by a merge) or dict
    col_widths: list
    page: int  # last page the table is on
    first_page: int
    padding: tuple  # pt (horizontal, vertical) between cell edges and their text
    height: float  # pt on the source page


@dataclass
class Figure:
    png: bytes
    width_pt: float
    page: int
    full_page: bool = False  # a scanned page with no text layer, kept as one image


@dataclass
class Redaction:
    page: int


@dataclass
class FrontHeading:
    title: str
    page: int


@dataclass
class Subheading:
    """An unnumbered heading inside a section's content."""
    title: str
    kind: str  # bold (08Subheading Bold) | label (Heading (No TOC))
    page: int = 0


@dataclass
class PageMark:
    landscape: bool
    page: int


@dataclass
class Document:
    title_rows: list = field(default_factory=list)
    title_tables: list = field(default_factory=list)
    front: list = field(default_factory=list)
    body: list = field(default_factory=list)


def advance(counters, level):
    del counters[level:]
    while len(counters) < level:
        counters.append(0)
    counters[-1] += 1
    return ".".join(str(counter) for counter in counters)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def merge_sources(source_lists: Iterable[list[JsonObject]]) -> list[JsonObject]:
    """One entry per v010 section, with the source indices of all entries for it united and sorted."""
    merged: dict[str, JsonObject] = {}
    for sources in source_lists:
        for source in sources:
            existing = merged.setdefault(source["section_id"], copy.deepcopy(source))
            existing["source_indices"] = sorted(set(existing["source_indices"]) | set(source["source_indices"]))
    return list(merged.values())


def cell_text(cell: dict | None) -> str:
    return " ".join(paragraph.text for paragraph in cell["paragraphs"]).strip() if cell else ""
