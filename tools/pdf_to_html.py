"""Convert an ICH M11 PDF to structured HTML, keeping tables, headings, bold/italic and page breaks.

Usage: python tools/pdf_to_html.py <in.pdf> <out.html> <title>
Each PDF page becomes <section id="pN">. Ruled tables become <table>; a table that runs onto the
next page is emitted again on that page (marked data-continued) so every page maps to its PDF page.
"""

import html
import re
import sys

import pymupdf

BOLD_FLAG = 16
ITALIC_FLAG = 2
LINE_GAP_FACTOR = 0.6  # a vertical gap above this x font size starts a new paragraph
HEADING_SIZE_STEP = {16: "h1", 13: "h2", 11.5: "h3"}  # minimum font size -> heading tag
BULLETS = ("•", "·", "§", "\uf0b7", "\uf0a7")  # \uf0b7 and \uf0a7 are Symbol-font bullets
LEADER_DOTS = re.compile(r"\.{5,}")  # table-of-contents entries


def span_html(span: dict) -> str:
    text = html.escape(span["text"], quote=False)
    if not text.strip():
        return text
    if span["flags"] & BOLD_FLAG or "bold" in span["font"].lower():
        text = f"<b>{text}</b>"
    if span["flags"] & ITALIC_FLAG or "italic" in span["font"].lower():
        text = f"<i>{text}</i>"
    return text


def line_html(line: dict) -> str:
    return re.sub(r" {2,}", " ", "".join(span_html(span) for span in line["spans"]))


def merge_adjacent(markup: str) -> str:
    for tag in ("b", "i"):
        markup = markup.replace(f"</{tag}><{tag}>", "").replace(f"</{tag}> <{tag}>", " ")
    return markup


def heading_tag(line: dict, body_size: float) -> str | None:
    size = max(span["size"] for span in line["spans"])
    text = "".join(span["text"] for span in line["spans"]).strip()
    all_bold = all(span["flags"] & BOLD_FLAG or "bold" in span["font"].lower() for span in line["spans"] if span["text"].strip())
    if not text or size < body_size + 1 or not all_bold:
        return None
    for minimum, tag in sorted(HEADING_SIZE_STEP.items(), reverse=True):
        if size >= minimum:
            return tag
    return "h4"


SAME_ROW_TOLERANCE = 8  # blocks whose tops fall in the same band of this height read left to right
TABLE_GAP_HEIGHT = 8  # a blank table row taller than this separates two tables; shorter ones are spacing
MIN_BORDER_LINES = 2  # a real table has drawn rules; one cut by a page break may show only two
BORDER_THICKNESS = 2
BORDER_MARGIN = 12  # the detected bbox can sit inside the drawn border
BORDER_MIN_LENGTH = 20
SEPARATE_LINE_SLACK = 40  # a line ending this far short of the cell's right edge was not wrapped


def cell_lines(page: pymupdf.Page, bbox) -> list[tuple[str, bool]]:
    """(html, wrapped) per PDF line in the cell; wrapped means the line runs to the cell's right edge."""
    rect = pymupdf.Rect(bbox)
    rect = pymupdf.Rect(rect.x0 + 0.5, rect.y0 + 0.5, rect.x1 - 0.5, rect.y1 - 0.5)
    lines = []
    for block in page.get_text("dict", clip=rect)["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            markup = merge_adjacent(line_html(line)).strip()
            if markup:
                lines.append((markup, line["bbox"][2] > rect.x1 - SEPARATE_LINE_SLACK))
    return lines


def join_lines(lines: list[tuple[str, bool]]) -> str:
    text = ""
    for index, (markup, wrapped) in enumerate(lines):
        text += markup
        if index < len(lines) - 1:
            text += " " if wrapped else "<br>"
    return " ".join(merge_adjacent(text).split(" ")).replace("  ", " ")


def border_line_count(page: pymupdf.Page, table) -> int:
    """Thin drawn segments (strokes or hairline fills) inside the table; highlights and shading have none."""
    box = pymupdf.Rect(table.bbox) + (-BORDER_MARGIN, -BORDER_MARGIN, BORDER_MARGIN, BORDER_MARGIN)
    count = 0
    for drawing in page.get_drawings():
        rect = drawing["rect"]
        if not box.contains(rect):
            continue
        thin = min(rect.width, rect.height) <= BORDER_THICKNESS
        long_enough = max(rect.width, rect.height) >= BORDER_MIN_LENGTH
        if thin and long_enough:
            count += 1
    return count


def is_table_of_contents(page: pymupdf.Page, table) -> bool:
    return bool(LEADER_DOTS.search(page.get_text(clip=pymupdf.Rect(table.bbox))))


def table_rows(page: pymupdf.Page, table) -> list[list[list[tuple[str, bool]]]]:
    """Logical rows: PDF rows whose first cell is empty continue the row above, blank rows end a table (None)."""
    logical: list = []
    for row in table.rows:
        cells = [cell_lines(page, bbox) if bbox else [] for bbox in row.cells]
        if not any(cells):
            if row.bbox[3] - row.bbox[1] > TABLE_GAP_HEIGHT:
                logical.append(None)
        elif not cells[0] and logical and logical[-1] is not None:
            for column, lines in enumerate(cells):
                logical[-1][column].extend(lines)
        else:
            logical.append(cells)
    return drop_empty_columns(logical)


def drop_empty_columns(rows: list) -> list:
    """Merged cells leave columns with no content in any row."""
    real_rows = [row for row in rows if row is not None]
    if not real_rows:
        return rows
    used = [column for column in range(len(real_rows[0])) if any(row[column] for row in real_rows)]
    return [None if row is None else [row[column] for column in used] for row in rows]


def tables_html(page: pymupdf.Page, table, continued: bool) -> list[tuple[tuple[float, float], str]]:
    """One <table> per blank-row-separated group; returns (y, html)."""
    groups: list[list] = [[]]
    for row in table_rows(page, table):
        if row is None:
            groups.append([])
        else:
            groups[-1].append(row)
    result = []
    for index, group in enumerate(g for g in groups if g):
        flag = " data-continued" if continued and index == 0 else ""
        body = "\n".join(
            "<tr>" + "".join(f"<td>{join_lines(cell)}</td>" for cell in row) + "</tr>" for row in group
        )
        result.append(((table.bbox[1] + index, table.bbox[0]), f"<table{flag}>\n{body}\n</table>"))
    return result


def body_font_size(doc: pymupdf.Document) -> float:
    counts: dict[float, int] = {}
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    size = round(span["size"], 1)
                    counts[size] = counts.get(size, 0) + len(span["text"])
    return max(counts, key=counts.get)


def text_blocks_outside(page: pymupdf.Page, table_boxes: list, body_size: float) -> list[tuple[tuple[float, float], str]]:
    """((y, x) position, html) for each paragraph or heading not inside a table."""
    items: list[tuple[tuple[float, float], str]] = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        lines = [
            line
            for line in block["lines"]
            if not any(pymupdf.Rect(line["bbox"]).intersects(box) and _mostly_inside(line["bbox"], box) for box in table_boxes)
        ]
        previous_bottom = None
        current: list[str] = []
        current_pos = (0.0, 0.0)
        current_tag = "p"
        for line in lines:
            markup = merge_adjacent(line_html(line))
            if not markup.strip():
                continue
            tag = heading_tag(line, body_size) or "p"
            line_size = max(span["size"] for span in line["spans"])
            gap = line["bbox"][1] - previous_bottom if previous_bottom is not None else 0
            starts_new = current and (
                tag != current_tag
                or gap > LINE_GAP_FACTOR * line_size
                or markup.lstrip().startswith(BULLETS)
                or LEADER_DOTS.search(current[-1])
            )
            if starts_new:
                items.append((current_pos, f"<{current_tag}>{' '.join(current)}</{current_tag}>"))
                current = []
            if not current:
                current_pos = (line["bbox"][1], line["bbox"][0])
                current_tag = tag
            current.append(markup.strip())
            previous_bottom = line["bbox"][3]
        if current:
            items.append((current_pos, f"<{current_tag}>{' '.join(current)}</{current_tag}>"))
    return items


def _mostly_inside(line_bbox, box) -> bool:
    overlap = pymupdf.Rect(line_bbox) & box
    area = pymupdf.Rect(line_bbox).get_area()
    return area > 0 and overlap.get_area() / area > 0.5


def convert(pdf_path: str, title: str) -> str:
    doc = pymupdf.open(pdf_path)
    body_size = body_font_size(doc)
    sections = []
    previous_table_open = False
    for page in doc:
        tables = [table for table in page.find_tables().tables if border_line_count(page, table) >= MIN_BORDER_LINES and not is_table_of_contents(page, table)]
        boxes = [pymupdf.Rect(table.bbox) for table in tables]
        items = text_blocks_outside(page, boxes, body_size)
        for index, table in enumerate(tables):
            continued = index == 0 and previous_table_open and table.bbox[1] < page.rect.y0 + 90
            items.extend(tables_html(page, table, continued))
        items.sort(key=lambda item: (round(item[0][0] / SAME_ROW_TOLERANCE), item[0][1]))
        last_table_bottom = max((table.bbox[3] for table in tables), default=0)
        previous_table_open = bool(tables) and last_table_bottom > page.rect.y1 - 110
        body = "\n".join(markup for _, markup in items)
        sections.append(f'<section id="p{page.number + 1}"><h6>Page {page.number + 1}</h6>\n{body}\n</section>')
    style = (
        "body{font:14px/1.4 sans-serif;max-width:1000px;margin:2em auto}"
        "section{border-top:2px solid #444;margin-top:2em}"
        "table{border-collapse:collapse;width:100%;margin:1em 0}"
        "td{border:1px solid #888;padding:4px;vertical-align:top}"
    )
    head = f"<!doctype html><html lang=en><head><meta charset=utf-8><title>{html.escape(title)}</title>\n<style>{style}</style></head><body>"
    return head + "\n" + "\n\n".join(sections) + "\n</body></html>\n"


if __name__ == "__main__":
    pdf_path, out_path, title = sys.argv[1:4]
    with open(out_path, "w", encoding="utf-8") as out_file:
        out_file.write(convert(pdf_path, title))
