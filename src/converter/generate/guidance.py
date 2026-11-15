"""Per-section drafting guidance and the ICH M11 format each section must follow.

The format comes from the M11 template (resources/research/m11_template_sections.json, built by
src/extract_m11_template.py) and the Technical Specification data elements (m11_elements.json). The spec's user
guidance and the CPT v011 key instructions fill in for sections the template has no entry for.
"""

import json
import re
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

RESEARCH_DIR = Path(__file__).resolve().parents[3] / "resources" / "research"
M11_GUIDANCE_PATH = RESEARCH_DIR / "m11_user_guidance.json"
M11_TEMPLATE_PATH = RESEARCH_DIR / "m11_template_sections.json"
M11_ELEMENTS_PATH = RESEARCH_DIR / "schema-inputs" / "m11_elements.json"
STYLE_EXAMPLES_PATH = RESEARCH_DIR / "m11_style_examples.json"
V11_INVENTORY_PATH = RESEARCH_DIR / "schema-inputs" / "v11_inventory.json"

REPEAT_PART = "X"
MAX_LISTED_VALUES = 12  # longer pick lists are summarised by count; the extractors offer them in full
CROSS_REFERENCE = re.compile(r"\bSections? (\d+(?:\.\d+)*)")

type JsonObject = dict[str, object]


@cache
def m11_guidance() -> dict[str, str]:
    return json.loads(M11_GUIDANCE_PATH.read_text())


@cache
def cpt_key_instructions() -> dict[str, list[str]]:
    inventory = json.loads(V11_INVENTORY_PATH.read_text())
    return {entry["number"]: entry["key_instructions"] for entry in inventory if entry.get("number")}


@cache
def style_examples() -> dict[str, str]:
    examples = json.loads(STYLE_EXAMPLES_PATH.read_text())
    return {number: text for number, text in examples.items() if not number.startswith("_")}


@cache
def template_sections() -> dict[str, JsonObject]:
    return json.loads(M11_TEMPLATE_PATH.read_text())


@cache
def elements_by_section() -> dict[str, list[JsonObject]]:
    result: dict[str, list[JsonObject]] = {}
    for element in json.loads(M11_ELEMENTS_PATH.read_text()):
        result.setdefault(element["m11_section"], []).append(element)
    return result


def section_guidance(number: str) -> list[str]:
    """The M11 spec's wording wins; the CPT key instructions are the fallback for sections the spec has no text for."""
    if number in m11_guidance():
        return [m11_guidance()[number]]
    return cpt_key_instructions().get(number, [])


def style_example(number: str) -> str | None:
    return style_examples().get(number)


# ---------------------------------------------------------------- M11 format

@dataclass
class SectionFormat:
    number: str
    title: str = ""
    optional: bool = False
    heading_only: bool = False
    items: list[JsonObject] = field(default_factory=list)
    elements: list[JsonObject] = field(default_factory=list)
    cross_refs: list[str] = field(default_factory=list)
    fallback_guidance: list[str] = field(default_factory=list)


def template_key(number: str, keys: set[str] | dict) -> str | None:
    """The M11 key for a protocol section number: itself, or the repeating form with X ("3.1.2" -> "3.1.X",
    "10.4.1.3" -> "10.4.X.3", "12.5" -> "12.X")."""
    number = number.split("#")[0]
    if number in keys:
        return number
    parts = number.split(".")
    for position in range(1, len(parts)):
        candidate = ".".join([*parts[:position], REPEAT_PART, *parts[position + 1:]])
        if candidate in keys:
            return candidate
    return None


@cache
def section_format(number: str) -> SectionFormat:
    templates = template_sections()
    key = template_key(number, templates)
    element_key = template_key(number, elements_by_section())
    elements = elements_by_section().get(element_key, []) if element_key else []
    if key is None:
        return SectionFormat(number, elements=elements, fallback_guidance=section_guidance(number),
                             cross_refs=element_cross_refs(elements, number))
    entry = templates[key]
    cross_refs = [*entry["cross_refs"]]
    cross_refs += [ref for ref in element_cross_refs(elements, key) if ref not in cross_refs]
    return SectionFormat(key, entry["title"], entry["optional"], entry["heading_only"], entry["items"], elements,
                         cross_refs)


def element_cross_refs(elements: list[JsonObject], own: str) -> list[str]:
    refs: list[str] = []
    for element in elements:
        for ref in CROSS_REFERENCE.findall(str(element.get("user_guidance") or "")):
            if ref != own and ref not in refs:
                refs.append(ref)
    return refs


def item_line(item: JsonObject) -> str:
    kind, text = item["kind"], str(item.get("text", ""))
    if kind == "instruction":
        return f"Guidance: {text}"
    if kind == "placeholder":
        return f"Content: {text}" + (" (optional)" if item.get("optional") else "")
    if kind == "fixed":
        return f'Fixed text, used word for word: "{text}"'
    if kind == "optional_text":
        return f"Optional fixed text: {text}"
    if kind == "or":
        return f"-- {text} --"
    if kind == "label":
        return f"Label: {item.get('line') or text}"
    if item.get("labels"):  # a form laid out as a grid: its cells are fields, not columns
        return f"Fields: {', '.join(item['labels'])}"
    columns = " | ".join(str(cell) for cell in item.get("header", []))
    line = f"Table: {columns}"
    if item.get("row_headers"):
        line += f"; row headings: {', '.join(item['row_headers'])}"
    return line


def normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def element_line(element: JsonObject) -> str:
    conformance = element.get("conformance") or {}
    rule = conformance.get("level", "")
    if conformance.get("condition"):
        rule += f" ({conformance['condition']})"
    details = [str(element.get("data_type") or ""), rule, f"cardinality {element.get('cardinality')}"]
    values = element.get("allowed_values") or []
    if values:
        names = [str(value["label"]) for value in values]
        details.append("one of: " + ", ".join(names) if len(names) <= MAX_LISTED_VALUES
                       else f"one of {len(names)} coded values")
    if element.get("repeatable"):
        details.append(f"repeats for {element.get('repeats_with') or 'each item'}")
    return f"- {element['element_name']} ({'; '.join(detail for detail in details if detail)})"


def format_lines(section: SectionFormat) -> list[str]:
    """The section's M11 format as prompt lines: template items in reading order, then the spec's data elements."""
    lines = []
    if section.optional:
        lines.append("This section is optional in M11.")
    if section.heading_only:
        lines.append("Heading only: M11 expects no text directly under this heading; content goes in its subsections.")
    lines.extend(item_line(item) for item in section.items)
    lines.extend(f"Guidance: {text}" for text in section.fallback_guidance)
    if section.elements:
        lines.append("Data elements (ICH M11 Technical Specification):")
        written = normalized(" ".join(str(item.get("text", "")) for item in section.items))
        for element in section.elements:
            lines.append(element_line(element))
            guidance = str(element.get("user_guidance") or "")
            if guidance and guidance != "N/A" and normalized(guidance) not in written:
                lines.append(f"  Guidance: {guidance}")
    if section.cross_refs:
        lines.append(f"Related M11 sections: {', '.join(section.cross_refs)}")
    return lines
