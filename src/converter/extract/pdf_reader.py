"""Read a protocol PDF into rows, paragraphs, tables, figures and redactions."""

import copy
import re
import statistics
from collections import Counter

import pymupdf

from converter.model import (
    BOLD_FLAG, DEFAULT_CELL_PADDING, ITALIC_FLAG, REDACTION_MARK, SUPERSCRIPT_FLAG, TOC_LEADER, Figure, PageMark,
    Paragraph, Redaction, Row, Span, Table, space_span, spans_text,
)


PAGE_MARGIN_ZONE = 0.12  # top/bottom fraction of a page treated as running header/footer
BOILERPLATE_MIN_PAGE_SHARE = 0.25  # a margin line repeated on this share of pages is a header/footer
BOILERPLATE_MIN_PAGES = 3  # a margin line must repeat at least this often, however short the document
SAME_ROW_TOLERANCE = 3.0  # pt; lines whose tops differ by less sit on the same row
PARAGRAPH_GAP_FACTOR = 1.6  # a vertical gap this many line heights apart starts a new paragraph
PARAGRAPH_EXTRA_GAP_FACTOR = PARAGRAPH_GAP_FACTOR - 1  # the same, measured from the previous row's bottom edge
BLOCK_BREAK_GAP_FACTOR = 0.2  # a gap this many line heights wide starts a new paragraph when the PDF block changes
DEFAULT_LINE_HEIGHT = 12  # pt, when no row has a measurable height
CELL_CENTER_SLACK = 1.5  # pt a text line's centre may stray outside the cell or rect that contains it
GRID_EDGE_MERGE_TOLERANCE = 2.0  # pt; cell edges closer than this are one grid line
TOC_PAGE_MIN_LEADER_SHARE = 0.25
MIN_FIGURE_SIDE = 40.0  # pt
MIN_VECTOR_FIGURE_AREA = 12000.0  # pt^2
REDACTION_FILL_SHARE = 0.8
BLACK = (0.0, 0.0, 0.0)
WHITE = (1.0, 1.0, 1.0)
REDACTION_BAR_HEIGHT_RANGE = (4.0, 16.0)  # pt; a black box about one text line tall is an inline redaction
MAX_CELL_PADDING = 10.0  # pt; a wider gap is centred or indented text, not padding
REDACTION_BAR_STROKE_SLACK = 1.0  # pt; a bar's outline sticks out past its rect
CELL_FILL_MIN_SHARE = 0.5  # a fill must cover this share of a cell to count as its shading
FIGURE_DPI = 150
BULLET_GLYPHS = {"•", "", "", "", "", "o", "▪", "■", "–", "-", "○", "◦", "§", "", "•", "", "✓"}


def line_text(line):
    return "".join(span["text"] for span in line["spans"])


def line_key(line):
    return normalize_boilerplate(line_text(line))


def spans_from_line(line):
    out = []
    for span in line["spans"]:
        if not span["text"]:
            continue
        flags = span["flags"]
        font = span["font"].lower()
        bold = bool(flags & BOLD_FLAG) or "bold" in font
        italic = bool(flags & ITALIC_FLAG) or "italic" in font
        out.append(Span(span["text"], bold, italic, bool(flags & SUPERSCRIPT_FLAG), round(span["size"], 1)))
    return out


def inside(bbox, rect, slack=CELL_CENTER_SLACK):
    cx = (bbox[0] + bbox[2]) / 2
    cy = (bbox[1] + bbox[3]) / 2
    return rect[0] - slack <= cx <= rect[2] + slack and rect[1] - slack <= cy <= rect[3] + slack


def overlaps(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def normalize_boilerplate(text):
    return re.sub(r"\d+", "#", text.strip().lower())


def find_boilerplate(pdf):
    counts = Counter()
    for page in pdf:
        height = page.rect.height
        seen = set()
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                top = line["bbox"][1]
                if PAGE_MARGIN_ZONE * height < top < (1 - PAGE_MARGIN_ZONE) * height:
                    continue
                key = line_key(line)
                if key:
                    seen.add(key)
        counts.update(seen)
    threshold = max(BOILERPLATE_MIN_PAGES, BOILERPLATE_MIN_PAGE_SHARE * pdf.page_count)
    return {key for key, count in counts.items() if count >= threshold}


def is_running_header(line, page, page_lines, boilerplate):
    """A header/footer line: in the page margin and repeated across pages.

    Numbers-only lines (page numbers, but also section numbers like '2.1') only count when
    nothing follows them on the same row, so a heading number next to its title is kept.
    """
    if not in_margin_zone(line["bbox"], page):
        return False
    key = line_key(line)
    if key not in boilerplate:
        return False
    if re.search(r"[a-z]", key):
        return True
    x1, y0 = line["bbox"][2], line["bbox"][1]
    return not any(abs(other["bbox"][1] - y0) < SAME_ROW_TOLERANCE and other["bbox"][0] >= x1
                   for other, _ in page_lines)


def in_margin_zone(bbox, page):
    height = page.rect.height
    return bbox[1] < PAGE_MARGIN_ZONE * height or bbox[3] > (1 - PAGE_MARGIN_ZONE) * height


def group_rows(lines, page_no):
    """Merge PDF lines sitting on the same baseline into rows, top to bottom."""
    lines = sorted(lines, key=lambda item: (round(item[0]["bbox"][1]), item[0]["bbox"][0]))
    rows = []
    for line, block_no in lines:
        bbox = line["bbox"]
        spans = spans_from_line(line)
        if not spans_text(spans).strip():
            continue
        row = rows[-1] if rows else None
        if row and abs(bbox[1] - row.y0) < SAME_ROW_TOLERANCE:
            row.pieces.append((bbox[0], spans))
            row.y1 = max(row.y1, bbox[3])
        else:
            rows.append(Row(page_no, bbox[0], bbox[1], bbox[3], block_no, [(bbox[0], spans)]))
    for row in rows:
        row.pieces.sort(key=lambda piece: piece[0])
        row.x0 = row.pieces[0][0]
    return rows


def is_toc_page(rows):
    if not rows:
        return False
    leaders = sum(1 for row in rows if TOC_LEADER.search(row.text))
    return leaders / len(rows) >= TOC_PAGE_MIN_LEADER_SHARE


def redaction_bars(drawings):
    return [drawing["rect"] for drawing in drawings
            if drawing.get("fill") == BLACK
            and REDACTION_BAR_HEIGHT_RANGE[0] <= drawing["rect"].height <= REDACTION_BAR_HEIGHT_RANGE[1]
            and drawing["rect"].width > drawing["rect"].height]


def find_tables(page, drawings):
    """Tables on the page, read from ruled lines only.

    Cell fills and inline redaction bars are left out: their edges would otherwise split rows
    and columns that the source draws as one cell.
    """
    bars = redaction_bars(drawings)
    if not bars:
        return page.find_tables(strategy="lines_strict").tables
    bar_rects = {tuple(rect) for rect in bars}
    ruling = [drawing for drawing in drawings
              if tuple(drawing["rect"]) not in bar_rects and not in_margin_zone(drawing["rect"], page)]
    if not ruling:
        # Redaction bars were the only line art; removing them below leaves artifacts that read as a table.
        return []
    scratch = pymupdf.open()
    try:
        scratch.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
        clean = scratch[0]
        slack = REDACTION_BAR_STROKE_SLACK
        for rect in bars:
            clean.add_redact_annot(rect + (-slack, -slack, slack, slack))
        clean.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                               graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                               text=pymupdf.PDF_REDACT_TEXT_NONE)
        return clean.find_tables(strategy="lines_strict").tables
    finally:
        scratch.close()


def cell_fill(cell, drawings):
    """Hex colour of the shading behind a cell, or None for white/unshaded cells."""
    area = (cell[2] - cell[0]) * (cell[3] - cell[1])
    for drawing in drawings:
        fill = drawing.get("fill")
        rect = drawing["rect"]
        if fill in (None, WHITE, BLACK) or not inside(tuple(cell), tuple(rect)):
            continue
        if rect.width * rect.height >= CELL_FILL_MIN_SHARE * area:
            return "".join(f"{round(channel * 255):02X}" for channel in fill[:3])
    return None


def cell_padding(table, lines):
    """Median gap between cell edges and the text inside them, as (horizontal, vertical)."""
    gaps_x, gaps_y = [], []
    for cell in table.cells:
        if not cell:
            continue
        inner = [line["bbox"] for line, _ in lines if inside(line["bbox"], cell)]
        if inner:
            gaps_x.append(min(bbox[0] for bbox in inner) - cell[0])
            gaps_y.append(min(bbox[1] for bbox in inner) - cell[1])

    def typical(gaps, default):
        plausible = [gap for gap in gaps if 0 <= gap <= MAX_CELL_PADDING]
        return statistics.median(plausible) if plausible else default

    return typical(gaps_x, DEFAULT_CELL_PADDING[0]), typical(gaps_y, DEFAULT_CELL_PADDING[1])


def table_grid(page, table, lines, drawings):
    """Turn a PyMuPDF table into a grid with explicit row/column spans."""
    xs = sorted({round(c[0], 1) for c in table.cells} | {round(c[2], 1) for c in table.cells})
    ys = sorted({round(c[1], 1) for c in table.cells} | {round(c[3], 1) for c in table.cells})
    xs = merge_close(xs)
    ys = merge_close(ys)
    n_cols, n_rows = len(xs) - 1, len(ys) - 1
    if n_cols < 1 or n_rows < 1:
        return None
    grid = [[None] * n_cols for _ in range(n_rows)]
    for cell in table.cells:
        c0, c1 = nearest(xs, cell[0]), nearest(xs, cell[2])
        r0, r1 = nearest(ys, cell[1]), nearest(ys, cell[3])
        if c1 <= c0 or r1 <= r0 or grid[r0][c0] is not None:
            continue
        cell_lines = [line_and_block for line_and_block in lines if inside(line_and_block[0]["bbox"], cell)]
        grid[r0][c0] = {
            "rowspan": r1 - r0,
            "colspan": c1 - c0,
            "paragraphs": cell_paragraphs(cell_lines, page.number + 1),
            "fill": cell_fill(cell, drawings),
        }
        for r in range(r0, r1):
            for c in range(c0, c1):
                if (r, c) != (r0, c0) and grid[r][c] is None:
                    grid[r][c] = "covered"
    for r in range(n_rows):
        for c in range(n_cols):
            if grid[r][c] is None:
                grid[r][c] = {"rowspan": 1, "colspan": 1, "paragraphs": [], "fill": None}
            elif grid[r][c] == "covered":
                grid[r][c] = None
    widths = [xs[i + 1] - xs[i] for i in range(n_cols)]
    return Table(grid, widths, page.number + 1, page.number + 1, cell_padding(table, lines),
                 table.bbox[3] - table.bbox[1])


def has_text(table):
    return any(cell and cell["paragraphs"] for row in table.grid for cell in row)


def merge_close(values, tolerance=GRID_EDGE_MERGE_TOLERANCE):
    out = []
    for value in values:
        if not out or value - out[-1] > tolerance:
            out.append(value)
    return out


def nearest(points, value):
    return min(range(len(points)), key=lambda i: abs(points[i] - value))


def cell_paragraphs(lines, page_no):
    rows = group_rows(lines, page_no)
    return rows_to_paragraphs(rows)


def starts_with_label(row):
    spans = [s for s in row.spans() if s.text.strip()]
    return bool(spans) and spans[0].bold and ":" in spans_text(s for s in spans if s.bold)


def rows_to_paragraphs(rows, break_on_labels=False):
    paragraphs = []
    line_heights = [row.height for row in rows if row.height > 0]
    typical = statistics.median(line_heights) if line_heights else DEFAULT_LINE_HEIGHT
    previous = None
    for row in rows:
        first = row.first_piece_text()
        bullet = first in BULLET_GLYPHS and len(row.pieces) > 1
        if not bullet and first[:1] in BULLET_GLYPHS - {"o", "-", "–"} and len(first) > 1:
            bullet = True
            row = strip_leading_glyph(row)
        new_paragraph = (
            previous is None
            or bullet
            or row.block != previous.block and row.y0 - previous.y1 > BLOCK_BREAK_GAP_FACTOR * typical
            or row.y0 - previous.y1 > PARAGRAPH_EXTRA_GAP_FACTOR * typical
            or starts_enumeration(row.text)
            or (break_on_labels and starts_with_label(row))
        )
        if new_paragraph:
            spans = row.spans(skip_pieces=1 if bullet and first in BULLET_GLYPHS else 0)
            kind = "bullet" if bullet else "body"
            paragraphs.append(Paragraph(spans, kind, page=row.page, first_x=row.x0))
        else:
            join_spans(paragraphs[-1].spans, row.spans())
        previous = row
    for para in paragraphs:
        para.spans = tidy_spans(para.spans)
    return [para for para in paragraphs if para.spans]


def tidy_spans(spans):
    """Collapse whitespace runs (including across span boundaries) and trim the paragraph ends."""
    out = []
    ends_with_space = True
    for span in spans:
        text = re.sub(r"\s+", " ", span.text)
        if ends_with_space:
            text = text.lstrip(" ")
        if not text:
            continue
        out.append(Span(text, span.bold, span.italic, span.superscript, span.size))
        ends_with_space = text.endswith(" ")
    while out and out[-1].text.endswith(" "):
        out[-1].text = out[-1].text.rstrip(" ")
        if not out[-1].text:
            out.pop()
    return out


def strip_leading_glyph(row):
    piece_x0, spans = row.pieces[0]
    spans = copy.deepcopy(spans)
    spans[0].text = spans[0].text.lstrip()[1:].lstrip()
    return Row(row.page, row.x0, row.y0, row.y1, row.block, [(piece_x0, spans)] + row.pieces[1:])


def starts_enumeration(text):
    return bool(re.match(r"^(\(?[a-z0-9]{1,3}[.)]|\[\d+\])\s+\S", text))


def join_spans(target, more):
    if not more:
        return
    last = target[-1].text if target else ""
    hyphenated = last.endswith("-") and not last.endswith(" -")
    if not hyphenated:
        target.append(space_span())
    target.extend(more)


def vector_figures(page, drawings, table_rects):
    figures = []
    for rect in page.cluster_drawings(drawings=drawings):
        rect = tuple(rect)
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        if width * height < MIN_VECTOR_FIGURE_AREA or in_margin_zone(rect, page):
            continue
        if any(overlaps(rect, table_rect) for table_rect in table_rects):
            continue
        figures.append(rect)
    return figures


def is_redaction(page, drawings, rect):
    words = page.get_text("words", clip=pymupdf.Rect(rect))
    if words and all(word[4] == REDACTION_MARK for word in words):
        return True
    area = (rect[2] - rect[0]) * (rect[3] - rect[1])
    filled = 0.0
    for drawing in drawings:
        drawing_rect = drawing["rect"]
        if drawing.get("fill") == BLACK and inside(tuple(drawing_rect), rect):
            filled += drawing_rect.width * drawing_rect.height
    return filled >= REDACTION_FILL_SHARE * area


def render_clip(page, rect):
    pix = page.get_pixmap(clip=pymupdf.Rect(rect), dpi=FIGURE_DPI)
    return pix.tobytes("png")


def read_pdf(path):
    items = []  # (page_no, y, kind, payload) in reading order
    toc_pages = set()
    with pymupdf.open(path) as pdf:
        boilerplate = find_boilerplate(pdf)
        for page in pdf:
            page_items, is_toc = read_page(page, boilerplate)
            items.extend(page_items)
            if is_toc:
                toc_pages.add(page.number + 1)
    items.sort(key=lambda item: (item[0], item[1]))
    return items, toc_pages


def read_page(page, boilerplate):
    """The page's items as (page_no, y, kind, payload), and whether it is a contents page."""
    page_no = page.number + 1
    blocks = page.get_text("dict")["blocks"]
    page_lines = [(line, block["number"]) for block in blocks for line in block.get("lines", [])
                  if line_text(line).strip()]
    lines = [(line, block_no) for line, block_no in page_lines
             if not is_running_header(line, page, page_lines, boilerplate)]
    landscape = page.rect.width > page.rect.height
    items = [(page_no, -1, "page", PageMark(landscape, page_no))]
    if not page_lines and page.get_images():
        scan = Figure(render_clip(page, page.rect), page.rect.width, page_no, full_page=True)
        items.append((page_no, 0, "figure", scan))
        return items, False
    all_rows = group_rows(lines, page_no)
    if is_toc_page(all_rows):
        # Contents pages are replaced by a TOC field; keep only their rows so headings like
        # "TABLE OF CONTENTS" still switch regions.
        items.extend((page_no, row.y0, "row", row) for row in all_rows)
        return items, True

    drawings = page.get_drawings()
    tables = [table for table in find_tables(page, drawings) if table.row_count >= 2 or table.col_count >= 2]
    taken = [tuple(table.bbox) for table in tables]
    for table in tables:
        table_lines = [line_and_block for line_and_block in lines if inside(line_and_block[0]["bbox"], table.bbox)]
        grid = table_grid(page, table, table_lines, drawings)
        if grid and has_text(grid):
            items.append((page_no, table.bbox[1], "table", grid))
    items.extend(figure_items(page, blocks, drawings, taken))

    free_lines = [line_and_block for line_and_block in lines
                  if not any(inside(line_and_block[0]["bbox"], rect) for rect in taken)]
    items.extend((page_no, row.y0, "row", row) for row in group_rows(free_lines, page_no))
    return items, False


def figure_items(page, blocks, drawings, taken):
    """Images and vector drawings outside `taken` (table rects), as figure or redaction items.

    Appends each found rect to `taken` so that text inside it is not read again as rows.
    """
    page_no = page.number + 1
    items = []
    for block in blocks:
        if block["type"] != 1:
            continue
        rect = tuple(block["bbox"])
        if rect[2] - rect[0] < MIN_FIGURE_SIDE or rect[3] - rect[1] < MIN_FIGURE_SIDE:
            continue
        if in_margin_zone(rect, page) or any(overlaps(rect, taken_rect) for taken_rect in taken):
            continue
        taken.append(rect)
        items.append((page_no, rect[1], "figure", Figure(render_clip(page, rect), rect[2] - rect[0], page_no)))
    for rect in vector_figures(page, drawings, taken):
        taken.append(rect)
        if is_redaction(page, drawings, rect):
            items.append((page_no, rect[1], "redaction", Redaction(page_no)))
        else:
            items.append((page_no, rect[1], "figure", Figure(render_clip(page, rect), rect[2] - rect[0], page_no)))
    return items
