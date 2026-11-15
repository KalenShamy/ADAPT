"""What the rendered docx must contain, derived from the protocol JSON in render order."""

from dataclasses import dataclass, field
from pathlib import Path

from converter.model import JsonObject, advance
from converter.protocol_fields import LEAD_INS, is_lead_in
from converter.render.docx import is_title_field
from converter.render.renumber import displayed_numbers, natural_key, renumber_text


PRE_LLM_SUFFIX = ".pre-llm"


@dataclass
class Expectation:
    """One block's text, whitespace-stripped, and where it came from (for the failure message)."""
    kind: str
    text: str
    where: str


@dataclass
class Audit:
    shifted_numbers: dict[str, str] = field(default_factory=dict)
    outline: list[tuple[int, str, str]] = field(default_factory=list)
    blocks: list[Expectation] = field(default_factory=list)
    figures: int = 0


def protocol_json_path(docx_path: Path) -> Path:
    if docx_path.stem.endswith(PRE_LLM_SUFFIX):
        return docx_path.with_name(docx_path.stem.removesuffix(PRE_LLM_SUFFIX) + ".json")
    return docx_path.with_name(docx_path.stem + ".generated.json")


def squash(text: str) -> str:
    """Run splits and line wrapping differ between JSON and docx, so compare without whitespace."""
    return "".join(text.split())


def table_text(rows: list[list[object]]) -> str:
    return "".join(str(cell) for row in rows for cell in row)


def build_audit(protocol: JsonObject) -> Audit:
    audit = Audit(shifted_numbers=displayed_numbers(protocol["sections"]))
    counters: list[int] = []
    for item in protocol.get("unplaced_v10_content", []):
        if not is_title_field(item):
            add_blocks(audit, item["blocks"], f"front matter {item['v10_source']['found_heading']!r}")
    sections = protocol["sections"]
    for key in sorted(sections, key=int):
        add_section(audit, counters, sections[key], level=1)
    return audit


def add_section(audit: Audit, counters: list[int], section: JsonObject, level: int) -> None:
    if section["status"] == "omitted":
        return
    number = str(section.get("number") or "")
    audit.outline.append((level, advance(counters, level), str(section["title"])))
    content = section.get("content", [])
    if number in LEAD_INS and content and is_lead_in(content[0]):
        content = content[1:]
    add_blocks(audit, content, f"section {number}")
    children = section.get("subsections", {})
    for key in sorted(children, key=natural_key):
        add_section(audit, counters, children[key], level + 1)
    for extra in section.get("additional_subsections", []):
        add_additional(audit, counters, extra, level + 1, number)


def add_additional(audit: Audit, counters: list[int], node: JsonObject, level: int, parent_number: str) -> None:
    audit.outline.append((level, advance(counters, level), str(node["title"])))
    add_blocks(audit, node["content"], f"additional subsection {node['title']!r} under {parent_number}")
    for child in node["subsections"]:
        add_additional(audit, counters, child, level + 1, parent_number)


def add_blocks(audit: Audit, blocks: list[JsonObject], where: str) -> None:
    for block in blocks:
        if block.get("has_image"):
            audit.figures += 1
            continue
        text = table_text(block["rows"]) if block["type"] == "table" else str(block.get("text", ""))
        text = renumber_text(text, audit.shifted_numbers)
        if squash(text):
            audit.blocks.append(Expectation(block["type"], squash(text), where))
