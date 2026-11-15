"""Clean up extracted blocks: captions, bullet levels, content split across pages, redactions."""

import re

from converter.extract.pdf_reader import join_spans
from converter.model import MAX_CAPTION_LENGTH, PageMark, Paragraph, Redaction, Table


BULLET_INDENT_STEP = 12.0  # pt of extra indent that makes a bullet one level deeper
CAPTION = re.compile(r"^(Table|Figure)\s+[\dA-Z][\w.\-]*:?\s+\S")


def classify_paragraphs(blocks):
    """Mark captions and set bullet nesting from indentation."""
    paragraphs = [block for block in blocks if isinstance(block, Paragraph)]
    bullet_x = [round(para.first_x) for para in paragraphs if para.kind == "bullet"]
    base = min(bullet_x) if bullet_x else 0
    for para in paragraphs:
        if para.kind == "bullet":
            para.level = 1 if para.first_x - base >= BULLET_INDENT_STEP else 0
        elif CAPTION.match(para.text) and len(para.text) <= MAX_CAPTION_LENGTH and ". " not in para.text[:-1]:
            para.kind = "caption"


def join_across_pages(blocks):
    """Rejoin paragraphs that a page break split mid-sentence."""
    out = []
    last_para = None
    for item in blocks:
        if isinstance(item, PageMark):
            out.append(item)
            continue
        if isinstance(item, Paragraph) and last_para is not None and item.page == last_para.page + 1 \
                and item.kind == "body" and last_para.kind in ("body", "bullet") \
                and last_para.text and last_para.text[-1] not in ".:;!?)" \
                and item.text[:1].islower():
            join_spans(last_para.spans, item.spans)
            last_para.page = item.page
            continue
        out.append(item)
        last_para = item if isinstance(item, Paragraph) else None
    return out


def merge_split_tables(blocks, by_repeated_header=False):
    """Join a table continued on the next page (same columns, nothing in between but a page break).

    A continuation may restate the table's caption and column headings; those are dropped.
    A Word source has no page numbers, so there a continuation is recognised by its repeated header row instead.
    """
    out = []
    for item in blocks:
        if isinstance(item, Table):
            table_index = continued_table_index(out)
            if table_index is not None:
                previous = out[table_index]
                if by_repeated_header:
                    continues = repeated_header_rows(previous.grid, item.grid) > 0
                else:
                    continues = item.page == previous.page + 1
                if len(previous.col_widths) == len(item.col_widths) and continues:
                    out[table_index + 1:] = [block for block in out[table_index + 1:] if isinstance(block, PageMark)]
                    previous.grid.extend(item.grid[repeated_header_rows(previous.grid, item.grid):])
                    previous.page = item.page
                    previous.first_page = min(previous.first_page, previous.page - 1)  # spans pages, so it can't be fitted onto one
                    continue
        out.append(item)
    return out


def continued_table_index(out):
    """Index of the table that the next table may continue: the last block, ignoring page marks
    and a caption restating the one above that table."""
    content = [index for index, block in enumerate(out) if not isinstance(block, PageMark)]
    if content and is_restated_caption(out, content):
        content.pop()
    if content and isinstance(out[content[-1]], Table):
        return content[-1]
    return None


def is_restated_caption(out, content):
    """The last content block is a caption with the same text as the one just above the table before it."""
    if len(content) < 3:
        return False
    caption, table, original = (out[index] for index in content[-1:-4:-1])
    return all(isinstance(block, Paragraph) and block.kind == "caption" for block in (caption, original)) \
        and isinstance(table, Table) and caption.text == original.text


def repeated_header_rows(table_rows, continuation_rows):
    """How many leading rows of the continuation repeat the table's own leading rows."""
    count = 0
    while count < min(len(table_rows), len(continuation_rows)) and same_row_text(continuation_rows[count], table_rows[count]):
        count += 1
    return count


def collapse_redactions(blocks):
    out = []
    for item in blocks:
        previous = next((block for block in reversed(out) if not isinstance(block, PageMark)), None)
        if isinstance(item, Redaction) and isinstance(previous, Redaction):
            continue
        out.append(item)
    return out


def same_row_text(first_row, second_row):
    def cell_texts(row):
        return [" ".join(para.text for para in cell["paragraphs"]) if cell else None for cell in row]
    return cell_texts(first_row) == cell_texts(second_row)
