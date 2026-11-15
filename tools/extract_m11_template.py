"""Extract the per-section format of the ICH M11 template, in reading order.

Usage: python tools/extract_m11_template.py <m11_template.html> <TechnicalSpecification.html> <out.json>

Both inputs are the PDF-to-HTML conversions in resources/reference/ich-m11/. The template gives each section's
instructions, placeholders, labels and tables in order; the template does not mark which sentences are fixed protocol
text, so those come from the Technical Specification's "Universal text" records.
Output maps an M11 section number to its format. Repeating sections use X ("3.1.X", "10.4.X.1", "12.X").
"""

import html
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

FIRST_SECTION_HEADING = "<h2><b>1</b>"
PAGE_MARKER = re.compile(r"<h6>Page \d+</h6>")
PAGE_NUMBER = re.compile(r"<p>\d+</p>\s*$")
SECTION_BLOCK = re.compile(r"<section[^>]*>(.*?)</section>", re.S)
ITEM = re.compile(r"<(h[1-5]|p)>(.*?)</\1>|<table[^>]*>(.*?)</table>", re.S)
ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL = re.compile(r"<td>(.*?)</td>", re.S)
BOLD = re.compile(r"<b>(.*?)</b>", re.S)
HEADING_NUMBER = re.compile(r"^(\d+(?:\.\d+)*|12\. ?X)\s+(.+)$")
REPEAT_MARK = "<#>"
HEADING_ONLY = "No text is intended here (heading only)"
CHOICE_WORDS = re.compile(r"^or\b", re.IGNORECASE)
MAX_CHOICE_WORDS = 6
PLACEHOLDER = re.compile(r"\{?<[^<>]+>\}?|\[[^\[\]]+\]")
MAX_WORDS_BESIDE_PLACEHOLDER = 3
OPTIONAL_SENTENCE = re.compile(r"^\{.*\}\.?$")  # a braced sentence the sponsor keeps or drops
SUB_BULLET = re.compile(r"^o\s")
SENTENCE_END = tuple(".:;?!)>}]\"”")
CROSS_REFERENCE = re.compile(r"\bSections? (\d+(?:\.\d+)*)")

# Tech spec record fields, as their bold labels read once adjacent bold runs are joined
SPEC_FIELDS = {
    "Term (Variable)": "term",
    "Data Type": "data_type",
    "Data (D), Value (V) or Heading (H)": "dvh",
    "Definition": "definition",
    "User Guidance": "user_guidance",
    "Conformance": "conformance",
    "Cardinality": "cardinality",
    "Relationship content from ToC representing the protocol hierarchy": "section",
    "Value": "value",
    "Business rules": "business_rules",
    "Repeating and/or Reuse Rules": "repeat_rule",
}
UNIVERSAL_TEXT = "universal text"
NOT_A_SENTENCE = {"", "N/A"}


def plain(fragment: str) -> str:
    text = re.sub(r"<br\s*/?>|</td>|</p>", " ", fragment)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\s+", " ", text).strip()


def normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# ---------------------------------------------------------------- technical specification records

def is_label_start(text: str) -> bool:
    return any(label.startswith(text) for label in SPEC_FIELDS)


def spec_tokens(spec_html: str) -> list[tuple[str, str]]:
    """("label", field name) and ("text", text) in document order.

    A field label can be split over several bold runs, and on some pages over several table rows with the field's
    value in between ("Data (D), Value (V) or" | D | "Heading (H)"). Runs are joined while they still spell the start
    of a label; text met in between is the value of that label once it is complete.
    """
    body = PAGE_MARKER.sub("", spec_html)
    tokens: list[tuple[str, str]] = []
    label_parts: list[str] = []
    values_between: list[str] = []
    position = 0

    def give_up_label() -> None:
        for part, value in zip(label_parts, [*values_between, ""]):
            tokens.extend([("text", part), ("text", value)])
        label_parts.clear()
        values_between.clear()

    for match in BOLD.finditer(body):
        between = plain(body[position:match.start()])
        bold = plain(match.group(1))
        position = match.end()
        if label_parts and is_label_start(" ".join([*label_parts, bold])):
            values_between.append(between)
            label_parts.append(bold)
        else:
            give_up_label()
            tokens.append(("text", between))
            label_parts.append(bold)
        joined = " ".join(label_parts)
        if joined in SPEC_FIELDS:
            tokens.append(("label", SPEC_FIELDS[joined]))
            tokens.extend(("text", value) for value in values_between)
            label_parts.clear()
            values_between.clear()
        elif not is_label_start(joined):
            give_up_label()
    give_up_label()
    tokens.append(("text", plain(body[position:])))
    return tokens


def spec_records(spec_html: str) -> list[dict[str, str]]:
    """Every "Term (Variable)" record, from both the table rows and the run-on paragraphs some pages use."""
    tokens = spec_tokens(spec_html)
    records: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    field_name: str | None = None
    for kind, value in tokens:
        if kind == "label":
            if value == "term":
                current = {}
                records.append(current)
            field_name = value
            if current is not None:
                current.setdefault(field_name, "")
        elif current is not None and field_name and value:
            current[field_name] = f"{current[field_name]} {value}".strip()
    return records


def universal_text_by_section(records: list[dict[str, str]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for record in records:
        value = record.get("value", "")
        # a Value of N/A marks a pick-list choice ("Not applicable"), not a sentence the protocol must carry
        if record.get("definition", "").lower() == UNIVERSAL_TEXT and value not in NOT_A_SENTENCE:
            section = record.get("section", "").split(";")[0].strip()
            result.setdefault(section, []).append(value)
    return result


# ---------------------------------------------------------------- template

@dataclass
class Section:
    number: str
    title: str
    optional: bool
    heading_only: bool = False
    items: list[dict[str, object]] = field(default_factory=list)
    cross_refs: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        return {"title": self.title, "optional": self.optional, "heading_only": self.heading_only,
                "items": self.items, "cross_refs": self.cross_refs}


def template_items(template_html: str) -> list[tuple[str, str]]:
    """(kind, raw html) for headings, paragraphs and tables from section 1 on, with page breaks removed and
    paragraphs split by a page break joined again."""
    start = template_html.index(FIRST_SECTION_HEADING)
    body = template_html[template_html.rfind("<section", 0, start):]
    items: list[tuple[str, str]] = []
    for block in SECTION_BLOCK.findall(body):
        block = PAGE_NUMBER.sub("", PAGE_MARKER.sub("", block).rstrip())
        for match in ITEM.finditer(block):
            tag, inner, table = match.group(1), match.group(2), match.group(3)
            if table is not None:
                items.append(("table", table))
            elif tag == "p" and items and continues(items[-1], inner):
                items[-1] = ("p", f"{items[-1][1]} {inner}")
            else:
                items.append(("h" if tag != "p" else "p", inner))
    return items


def continues(previous: tuple[str, str], inner: str) -> bool:
    """A paragraph the PDF conversion broke at a line or page end: the earlier part has no sentence end and this
    part starts in lower case (the template's "o" sub-bullets also start in lower case and are kept apart)."""
    text = plain(inner)
    return (previous[0] == "p" and not plain(previous[1]).endswith(SENTENCE_END)
            and text[:1].islower() and not SUB_BULLET.match(text))


def is_all_bold(inner: str) -> bool:
    return bool(BOLD.search(inner)) and not plain(BOLD.sub("", inner))


def bold_prefix(inner: str) -> tuple[str, str]:
    """(text of the bold runs a paragraph opens with, the rest of the paragraph)."""
    end = 0
    for match in BOLD.finditer(inner):
        if plain(inner[end:match.start()]):
            break
        end = match.end()
    return plain(inner[:end]), inner[end:]


def heading_of(kind: str, inner: str) -> tuple[str, str, str] | None:
    """(number, title, trailing html) of a heading; a deeper heading is a paragraph opening with a bold number and
    title, sometimes followed by its first instruction in the same paragraph."""
    if kind != "p":
        match = HEADING_NUMBER.match(plain(inner))
        return (match.group(1).replace(" ", ""), match.group(2).strip(), "") if match else None
    prefix, rest = bold_prefix(inner)
    match = HEADING_NUMBER.match(prefix)
    if not match or "." not in match.group(1):
        return None
    return match.group(1).replace(" ", ""), match.group(2).strip(), rest


def leading_bold(inner: str) -> str:
    """The bold text a paragraph or cell opens with, joined across runs, up to and including a colon."""
    text = ""
    for match in BOLD.finditer(inner):
        if plain(inner[:match.start()]) != plain(text) and plain(inner[:match.start()]):
            break
        text = f"{text} {plain(match.group(1))}".strip()
        if ":" in text:
            return text[:text.index(":") + 1]
    return text


def bold_groups(inner: str) -> list[tuple[str, str]]:
    """(bold text, plain text up to the next bold group) for each run of adjacent bold segments."""
    groups: list[tuple[str, str]] = []
    texts: list[str] = []
    end = 0
    for match in BOLD.finditer(inner):
        if texts and plain(inner[end:match.start()]):
            groups.append((" ".join(texts), plain(inner[end:match.start()])))
            texts = []
        texts.append(plain(match.group(1)))
        end = match.end()
    if texts:
        groups.append((" ".join(texts), plain(inner[end:])))
    return groups


def cell_labels(cell: str) -> list[str]:
    """Field labels in a form-like cell: bold text ending in a colon, or bold text right before a [pick-list]."""
    return [text if text.endswith(":") else f"{text}:" for text, after in bold_groups(cell)
            if text.endswith(":") or after.startswith("[")]


def table_item(table: str) -> dict[str, object]:
    rows = [[plain(cell) for cell in CELL.findall(row)] for row in ROW.findall(table)]
    cells = [cell for row in ROW.findall(table) for cell in CELL.findall(row)]
    labels = [label for cell in cells for label in cell_labels(cell)]
    item: dict[str, object] = {"kind": "table", "header": [cell for cell in rows[0] if cell] if rows else []}
    row_headers = [leading_bold(CELL.findall(row)[0]) for row in ROW.findall(table)[1:] if CELL.findall(row)]
    if any(row_headers):
        item["row_headers"] = [header for header in row_headers if header]
    if labels:
        item["labels"] = labels
    return item


def paragraph_item(inner: str, universal: set[str]) -> dict[str, object] | None:
    text = plain(inner)
    if not text:
        return None
    if normalized(text) in universal:
        return {"kind": "fixed", "text": text}
    if CHOICE_WORDS.match(text) and len(text.split()) <= MAX_CHOICE_WORDS:
        return {"kind": "or", "text": text}
    label = leading_bold(inner)
    if label and (label.endswith(":") or is_all_bold(inner)):
        return {"kind": "label", "text": label.rstrip(":").strip() if is_all_bold(inner) else label, "line": text}
    remainder = PLACEHOLDER.sub("", text)
    if PLACEHOLDER.search(text) and len(remainder.split()) <= MAX_WORDS_BESIDE_PLACEHOLDER:
        return {"kind": "placeholder", "text": text, "optional": text.startswith("{")}
    if OPTIONAL_SENTENCE.match(text):
        return {"kind": "optional_text", "text": text}
    return {"kind": "instruction", "text": text}


def section_number(number: str, title: str, repeat_prefixes: dict[str, str]) -> str:
    """Repeating sections ("Primary Objective <#>") and their subsections take X in place of their number."""
    if number.startswith("12.") and number.endswith("X"):
        return "12.X"
    for prefix, repeated in repeat_prefixes.items():
        if number.startswith(prefix + "."):
            return repeated + number[len(prefix):]
    if REPEAT_MARK in title:
        repeated = number.rsplit(".", 1)[0] + ".X"
        repeat_prefixes[number] = repeated
        return repeated
    return number


def template_sections(template_html: str, universal_by_section: dict[str, list[str]]) -> dict[str, Section]:
    universal = {normalized(text) for texts in universal_by_section.values() for text in texts}
    sections: dict[str, Section] = {}
    repeat_prefixes: dict[str, str] = {}
    current: Section | None = None
    for kind, inner in template_items(template_html):
        heading = heading_of(kind, inner) if kind != "table" else None
        if heading:
            number, raw_title, inner = heading
            title = raw_title.strip("{} ")
            number = section_number(number, raw_title, repeat_prefixes)
            current = sections.setdefault(number, Section(number, title, raw_title.startswith("{")))
            if not plain(inner):
                continue
        if current is None:
            continue
        if kind == "table":
            current.items.append(table_item(inner))
            continue
        if plain(inner).startswith(HEADING_ONLY):
            current.heading_only = True
            continue
        item = paragraph_item(inner, universal)
        if item:
            current.items.append(item)
        for reference in CROSS_REFERENCE.findall(plain(inner)):
            if reference != current.number and reference not in current.cross_refs:
                current.cross_refs.append(reference)
    for section in sections.values():
        if not section.items:  # section 9 has no text under its heading and no "heading only" note
            section.heading_only = True
    for number, texts in universal_by_section.items():
        section = sections.get(number)
        written = {normalized(str(item.get("text", ""))) for item in section.items} if section else set()
        for text in texts:
            if section and normalized(text) not in written:
                section.items.append({"kind": "fixed", "text": text})
    return sections


def main() -> None:
    template_path, spec_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    template_html = Path(template_path).read_text(encoding="utf-8")
    spec_html = Path(spec_path).read_text(encoding="utf-8")
    universal = universal_text_by_section(spec_records(spec_html))
    sections = template_sections(template_html, universal)
    with open(out_path, "w", encoding="utf-8") as out_file:
        json.dump({number: section.as_json() for number, section in sections.items()}, out_file,
                  indent=1, ensure_ascii=False)
        out_file.write("\n")
    print(f"{len(sections)} sections")


if __name__ == "__main__":
    main()
