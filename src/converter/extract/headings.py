"""Find numbered headings in extracted rows and structure them into front matter and body."""

import re
from collections import Counter

from converter.extract.pdf_reader import rows_to_paragraphs
from converter.model import (
    MISSING_HEADING_TITLE, REDACTED_HEADING_TITLE, TOC_LEADER, Document, FrontHeading, Heading, PageMark, spans_text,
)


TITLE_PAGE_LAST = 1  # later pages in the title region are front matter (e.g. an investigator agreement)
SECTION_NUMBER = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,5})\.?$")
COMBINED_HEADING = re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,5})\.?\s+([A-Z].*)$")
MAX_SKIPPED_HEADINGS = 3  # sections a heading may skip (eg redacted ones) and still count as the next heading
WRAPPED_TITLE_MAX_INDENT = 60.0  # pt
WRAPPED_TITLE_LEFT_SLACK = 2.0  # pt a wrapped line may start left of the number's left edge
WRAPPED_TITLE_X_TOLERANCE = 16.0  # pt a wrapped title line may differ from the first title line's left edge
WRAPPED_TITLE_SIZE_TOLERANCE = 0.5  # pt
HEADING_X_TOLERANCE = 30.0  # pt a heading may sit right of the first heading's left edge
FRONT_HEADING_SIZE_MARGIN = 0.5  # pt a front-matter heading must exceed the body size by to count as larger
DEFAULT_BODY_FONT_SIZE = 12  # pt, when no row has text
APPENDIX_HEADING = re.compile(r"^Appendix\s+([A-Z])\b[\s:.]+\S")
LETTERED_SUBSECTION = re.compile(r"^([A-Z])\s?(\d{1,2}(?:\.\d{1,2}){0,3})$")

FRONT_MATTER_HEADINGS = re.compile(
    r"^(document history|protocol amendment summary of changes( table)?|summary of changes|"
    r"overall rationale for the amendment|amended clinical trial protocol \d+|protocol history|"
    r"assessment of substantiality|overview of changes.*|amendment \d+ \(.*\)|amendment \d+|"
    r"list of abbreviations.*|abbreviations.*|definitions of terms|sponsor signatory|"
    r"signature page.*|investigator'?s? (agreement|signature|statement).*|protocol (amendment )?acceptance form|"
    r"confidentiality statement|protocol approval|table of contents|contents|list of tables|"
    r"list of figures|list of appendices)$",
    re.IGNORECASE,
)

SKIPPED_FRONT_MATTER = re.compile(r"^(table of contents|contents|list of tables|list of figures|list of appendices)$", re.IGNORECASE)


def skipped_numbers(previous, number):
    """Section numbers missing between `previous` and `number`, or None if `number` can't follow it.

    An empty list means `number` is the direct successor. When `number` lies under `previous` but
    more than one level deeper, the skipped headings include the levels in between (8.10 then
    8.10.1.3 skips 8.10.1, 8.10.1.1 and 8.10.1.2).
    """
    parts = [int(part) for part in number.split(".")]
    previous_parts = [int(part) for part in previous.split(".")] if previous else []
    parent = parts[:-1]
    if previous_parts and len(parent) > len(previous_parts) and parts[:len(previous_parts)] == previous_parts:
        return skipped_below(previous_parts, parts)
    if len(parent) > len(previous_parts) or previous_parts[:len(parent)] != parent:
        return None
    start = previous_parts[len(parent)] + 1 if len(previous_parts) > len(parent) else 1
    if parts[-1] < start:
        return None
    return [".".join(map(str, parent + [sibling])) for sibling in range(start, parts[-1])]


def skipped_below(previous_parts, parts):
    skipped = []
    for depth in range(len(previous_parts) + 1, len(parts) + 1):
        prefix = parts[:depth - 1]
        skipped.extend(".".join(map(str, prefix + [sibling])) for sibling in range(1, parts[depth - 1]))
        if depth < len(parts):
            skipped.append(".".join(map(str, parts[:depth])))
    return skipped


def heading_candidate(row, last_number):
    """A row starting with a section number followed by title text, eg '2.1' + 'Study Rationale'.

    Returns (number, title, combined); combined is True when number and title share one text run.
    Lettered appendices ('Appendix A ...', then 'A 1' + title) are numbered under the current
    level-1 section, the way CPT numbers its appendices (10.1, 10.1.1, ...).
    """
    first = row.first_piece_text()
    rest = " ".join(spans_text(spans).strip() for _, spans in row.pieces[1:]).strip()
    chapter = last_number.split(".")[0] if last_number else None
    match = SECTION_NUMBER.match(first)
    appendix = APPENDIX_HEADING.match(row.text)
    appendix_sub = LETTERED_SUBSECTION.match(first)
    if match and rest:
        number, title, combined = match.group(1), rest, False
    elif match and row.is_bold() and "." in match.group(1) and len(row.pieces) == 1:
        number, title, combined = match.group(1), REDACTED_HEADING_TITLE, False
    elif appendix and chapter:
        number = f"{chapter}.{letter_index(appendix.group(1))}"
        title, combined = row.text.strip(), True
    elif appendix_sub and rest and chapter:
        number = f"{chapter}.{letter_index(appendix_sub.group(1))}.{appendix_sub.group(2)}"
        title, combined = rest, False
    else:
        match = COMBINED_HEADING.match(row.text)
        if not match:
            return None
        number, title, combined = match.group(1), match.group(2).strip(), True
    if not title or TOC_LEADER.search(title) or not re.match(r"[A-Za-z\[(]", title):
        return None
    return number, title, combined


def letter_index(letter):
    return ord(letter.upper()) - ord("A") + 1


def section_sort_key(number):
    return [int(part) for part in number.split(".")]


def bookmark_gap(bookmarks, last_position, number, emitted):
    """Bookmarked headings listed after the last emitted one and before `number` that the text never showed."""
    numbers = [bookmark_number for bookmark_number, _, _ in bookmarks]
    position = numbers.index(number)
    return [bookmarks[index] for index in range(last_position + 1, position) if numbers[index] not in emitted]


def structure(items, toc_pages, body_size, bookmarks=()):
    """Split rows into title, front matter and body.

    `bookmarks` is [(number, title, page)] from the PDF outline. A heading the outline places on
    its own page is accepted without a numbering chain, and bookmarked headings the text lacks
    (under a black box, on a scanned page) become placeholders carrying the bookmark's title.
    """
    doc = Document()
    bookmark_pages = {(number, page) for number, _, page in bookmarks}
    bookmark_titles = {number: title for number, title, _ in reversed(bookmarks)}
    bookmark_numbers = [number for number, _, _ in bookmarks]
    emitted, last_position = set(), -1
    landscape_pages = {page_no: payload.landscape for page_no, _, kind, payload in items if kind == "page"}
    region = "title"
    last_number = None
    pending_rows = []
    heading_x = {}  # left edge of headings, per page orientation

    def flush():
        if not pending_rows:
            return
        rows = list(pending_rows)
        pending_rows.clear()
        if region == "title":
            doc.title_rows.extend(rows)
        elif region == "front":
            doc.front.extend(rows_to_paragraphs(rows))
        elif region == "body":
            doc.body.extend(rows_to_paragraphs(rows))

    i = 0
    while i < len(items):
        page_no, _, kind, payload = items[i]
        i += 1
        if kind == "page":
            flush()
            if region == "body":
                doc.body.append(payload)
            continue
        if region == "title" and page_no > TITLE_PAGE_LAST:
            flush()
            region = "front"
        if kind != "row":
            flush()
            if region == "title":
                if kind == "table":
                    doc.title_tables.append(payload)
            elif region == "front":
                doc.front.append(payload)
            elif region == "body":
                doc.body.append(payload)
            continue

        row = payload
        candidate = heading_candidate(row, last_number)
        skipped = skipped_numbers(last_number, candidate[0]) if candidate else None
        emphasized = row.is_bold() or row.size() > body_size
        landscape = landscape_pages[page_no]
        x_ok = landscape not in heading_x or abs(row.x0 - heading_x[landscape]) < HEADING_X_TOLERANCE
        styled_differently = emphasized or row.is_italic()
        redacted_title = bool(candidate) and candidate[1] == REDACTED_HEADING_TITLE
        bookmarked = bool(candidate) and (candidate[0], page_no) in bookmark_pages and emphasized \
            and candidate[0] not in emitted
        chain_ok = bool(candidate) and skipped is not None \
            and not (redacted_title and skipped) \
            and (emphasized or (not candidate[2] and "." in candidate[0])
                 or (styled_differently and not skipped and candidate[0].count(".") >= 2)) \
            and (not skipped or (emphasized and len(skipped) <= MAX_SKIPPED_HEADINGS))
        if candidate and page_no not in toc_pages and x_ok and (bookmarked or chain_ok):
            heading_x.setdefault(landscape, row.x0)
            flush()
            number, title, combined = candidate
            # Long headings wrap onto following rows of the same block, aligned with the title text
            # (or anywhere in the hanging indent when number and title share one run).
            if not redacted_title:
                for wrapped in wrapped_title_rows(items, i, row, combined):
                    title += " " + wrapped.text
                    i += 1
                    row = wrapped
            if region != "body":
                region = "body"
                doc.body.append(PageMark(landscape, page_no))
            missing_headings = {missing: (bookmark_titles.get(missing, MISSING_HEADING_TITLE), page_no)
                                for missing in skipped or [] if missing not in emitted}
            if bookmarked:
                missing_headings.update({gap_number: (gap_title, gap_page) for gap_number, gap_title, gap_page
                                         in bookmark_gap(bookmarks, last_position, number, emitted)})
            for missing in sorted(missing_headings, key=section_sort_key):
                doc.body.append(Heading(missing, *missing_headings[missing]))
                emitted.add(missing)
            doc.body.append(Heading(number, title, page_no))
            emitted.add(number)
            if number in bookmark_numbers:
                last_position = bookmark_numbers.index(number)
            last_number = number
            continue

        if region in ("title", "front", "skip") and is_front_heading(row, body_size):
            flush()
            if SKIPPED_FRONT_MATTER.match(row.text.strip()):
                region = "skip"
            else:
                region = "front"
                doc.front.append(FrontHeading(row.text.strip().title() if row.text.isupper() else row.text.strip(), page_no))
            continue
        if region == "skip" and page_no not in toc_pages and not TOC_LEADER.search(row.text):
            # The contents list ended without a new heading; resume front matter.
            region = "front"
        if page_no in toc_pages:
            continue
        pending_rows.append(row)
    flush()
    return doc


def wrapped_title_rows(items, start, row, combined):
    """Rows after `row` (at items[start]) that continue its heading title.

    Long headings wrap onto following rows of the same block, aligned with the title text
    (or anywhere in the hanging indent when number and title share one run).
    """
    title_x = None if combined else row.pieces[1][0]
    wrapped = []
    for _, _, kind, candidate in items[start:]:
        if kind != "row":
            break
        aligned = (row.x0 - WRAPPED_TITLE_LEFT_SLACK <= candidate.x0 <= row.x0 + WRAPPED_TITLE_MAX_INDENT) if combined \
            else abs(candidate.x0 - title_x) < WRAPPED_TITLE_X_TOLERANCE
        if candidate.page == row.page and (candidate.block == row.block or not combined) and aligned \
                and candidate.y0 - row.y1 < row.height \
                and candidate.starts_bold() == row.is_bold() \
                and abs(candidate.size() - row.size()) < WRAPPED_TITLE_SIZE_TOLERANCE:
            wrapped.append(candidate)
            row = candidate
        else:
            break
    return wrapped


def is_front_heading(row, body_size):
    if TOC_LEADER.search(row.text):
        return False
    if not (row.is_bold() or row.size() > body_size + FRONT_HEADING_SIZE_MARGIN):
        return False
    return bool(FRONT_MATTER_HEADINGS.match(row.text.strip()))


def body_font_size(items):
    sizes = Counter()
    for _, _, kind, payload in items:
        if kind == "row":
            for _, spans in payload.pieces:
                for span in spans:
                    sizes[span.size] += len(span.text)
    return sizes.most_common(1)[0][0] if sizes else DEFAULT_BODY_FONT_SIZE
