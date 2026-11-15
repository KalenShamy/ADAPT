"""Heading numbers as Word shows them once omitted optional sections are dropped, and the references to them.

Word numbers headings itself, so a skipped section shifts its later siblings up. The M11 template says to
renumber the remaining headings; the table of contents and "Section N" references follow the shifted numbers.
"""

import re

from cross_references import OTHER_DOCUMENT, REFERENCE, block_paragraphs, list_numbers, replace_text
from pdf_to_cpt_docx import advance

NUMBER_PART = re.compile(r"^[\d.]+")
# Schema sections with no number of their own that the CPT template still styles as a numbered Heading 4
NUMBERED_TITLES_WITHOUT_M11_NUMBER = {"Time Period for Detecting Medical Device Deficiencies"}

type JsonObject = dict[str, object]
Edit = tuple[int, int, str]


def ordered_children(section: JsonObject) -> list[JsonObject]:
    """The sections written under `section`, in the order they are written."""
    children = section.get("subsections", {})
    ordered = [children[key] for key in sorted(children, key=lambda key: natural_key(str(children[key].get("number") or key)))]
    analyses = [analysis for analysis in section.get("objective_analyses", []) if analysis["status"] == "populated"]
    return [*ordered, *analyses]


def is_sub_label(section: JsonObject) -> bool:
    """An M11 unnumbered heading (9.2.2 Recording): written as a label that takes no heading number."""
    return not section.get("number") and section.get("title") not in NUMBERED_TITLES_WITHOUT_M11_NUMBER


def natural_key(key: str) -> tuple[int, ...]:
    match = NUMBER_PART.match(key)
    return tuple(int(part) for part in match.group(0).split(".") if part) if match else ()


def displayed_numbers(sections: JsonObject) -> dict[str, str]:
    """Template number -> the number Word shows, for each written section whose number shifts."""
    counters: list[int] = []
    shifted: dict[str, str] = {}

    def visit(section: JsonObject, level: int) -> None:
        if section["status"] == "omitted" or is_sub_label(section):
            return
        number = section.get("number")
        shown = advance(counters, level)
        if number and str(number) != shown:
            shifted[str(number)] = shown
        for child in ordered_children(section):
            visit(child, level + 1)

    for key in sorted(sections, key=int):
        visit(sections[key], 1)
    return shifted


def renumber_edits(text: str, shifted: dict[str, str]) -> list[Edit]:
    edits: list[Edit] = []
    for match in REFERENCE.finditer(text):
        if OTHER_DOCUMENT.match(text, match.end()):
            continue
        for start, end, number in referenced_numbers(match):
            if number in shifted:
                edits.append((start, end, shifted[number]))
    return edits


def referenced_numbers(match) -> list[Edit]:
    """(start, end, number) of each section number a reference match names; appendix numbers are not sections."""
    for name in ("paren_number", "bracket_number", "plain_number", "section_first"):
        if match.group(name):
            return [(match.start(name), match.end(name), match.group(name))]
    if match.group("section_list"):
        plural = match.group("section_word").lower().endswith("s")
        return list_numbers(match, "section_list", plural)
    return []


def renumber_text(text: str, shifted: dict[str, str]) -> str:
    for start, end, new_number in sorted(renumber_edits(text, shifted), reverse=True):
        text = text[:start] + new_number + text[end:]
    return text


def renumber_blocks(blocks: list, shifted: dict[str, str]) -> None:
    """Rewrite the references in rich blocks (paragraphs and table cells) in place."""
    if not shifted:
        return
    for paragraph in block_paragraphs(blocks):
        text = "".join(span.text for span in paragraph.spans)
        for start, end, new_number in sorted(renumber_edits(text, shifted), reverse=True):
            replace_text(paragraph.spans, start, end, new_number)
