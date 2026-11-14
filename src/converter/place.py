"""Place a v010 Document into the v011-shaped protocol JSON (schema/protocol.schema.json).

Text goes into the JSON. Rich layout (spans, merged cells, figures) goes into a sidecar keyed by
each block's `source_index`, which for PDF input is the block's position in the placed order.
"""

import copy
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from block_xhtml import blocks_to_xhtml
from pdf_to_cpt_docx import Figure, Paragraph, Redaction, Table
from restructure import Subheading, iter_nodes, restructure

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schema"
sys.path.insert(0, str(SCHEMA_DIR))
from test_protocol_schema import minimal_instance, v10_source  # noqa: E402  (skeleton builder lives with the schema tests)

type JsonObject = dict[str, object]

SOURCE_TEMPLATE = "CPT v010"
TARGET_TEMPLATE = "CPT v011"
SCRIPT_ACTOR: JsonObject = {"kind": "script", "name": "converter.place", "version": "1"}
SUBHEADING_LEVEL = 4
REDACTED_TEXT = "[REDACTED]"
FIGURE_TEXT = "[Figure]"
BULLET_STYLE = "List Paragraph"
BODY_STYLE = "Normal"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def paragraph_text(paragraph: Paragraph) -> str:
    return "".join(span.text for span in paragraph.spans).strip()


def table_rows(table: Table) -> list[list[str]]:
    return [
        ["\n".join(paragraph_text(p) for p in cell["paragraphs"]) if cell else "" for cell in row]
        for row in table.grid
    ]


def custody(origin: str, sources: list[JsonObject]) -> JsonObject:
    return {
        "origin": origin,
        "v10_sources": copy.deepcopy(sources),
        "actor": copy.deepcopy(SCRIPT_ACTOR),
        "confidence": "high",
        "needs_review": False,
    }


class Placer:
    def __init__(self, assets_dir: Path) -> None:
        self.assets_dir = assets_dir
        self.next_index = 0
        self.layout: dict[str, str] = {}

    def block(self, block: object, section_source: JsonObject) -> JsonObject:
        index = self.next_index
        self.next_index += 1
        self.layout[str(index)] = blocks_to_xhtml([block], self.assets_dir)
        source = {**section_source, "source_indices": [index], "portion": "all"}
        base = {"source_index": index, "custody": custody("v10_derived", [source])}
        if isinstance(block, Table):
            return {"type": "table", "rows": table_rows(block), **base}
        if isinstance(block, Subheading):
            return {"type": "heading", "text": block.title, "level": SUBHEADING_LEVEL, **base}
        if isinstance(block, Figure):
            return {"type": "paragraph", "text": FIGURE_TEXT, "style": BODY_STYLE, "has_image": True, **base}
        if isinstance(block, Redaction):
            return {"type": "paragraph", "text": REDACTED_TEXT, "style": BODY_STYLE, **base}
        style = BULLET_STYLE if block.kind == "bullet" else BODY_STYLE
        return {"type": "paragraph", "text": paragraph_text(block), "style": style, **base}

    def section_content(self, node: object, ancestor_number: str) -> tuple[list[JsonObject], list[JsonObject]]:
        """Content blocks and the section's V10Sources for one output node."""
        if node.from_template:
            source_number = node.source_numbers[0] if node.source_numbers else None
            match = "exact"
            section_id = f"v10:{source_number}" if source_number else f"v10:{ancestor_number}.unmapped"
        else:
            source_number = node.source_numbers[0] if node.source_numbers else None
            match = "protocol_added"
            section_id = f"v10:{ancestor_number}.unmapped"
        section_source = {
            "section_id": section_id,
            "found_number": source_number,
            "found_heading": node.source_titles[0] if node.source_titles else node.title,
            "match": match,
        }
        blocks = [self.block(block, section_source) for block in node.blocks]
        sources = merge_sources(block["custody"]["v10_sources"] for block in blocks)
        return blocks, sources

    def additional_subsection(self, node: object, ancestor_number: str) -> JsonObject:
        blocks, sources = self.section_content(node, ancestor_number)
        children = [self.additional_subsection(child, ancestor_number) for child in node.children]
        heading_source = {**(sources[0] if sources else self.heading_only_source(node, ancestor_number)), "match": "protocol_added", "portion": "all"}
        heading_source["section_id"] = f"v10:{ancestor_number}.unmapped"
        return {
            "number": None, "title": node.title, "level": node.level,
            "status": "populated" if blocks else "container",
            "heading_source": heading_source,
            "custody": custody("v10_derived", sources) if blocks else custody("none", []),
            "content": blocks, "subsections": children,
        }

    def heading_only_source(self, node: object, ancestor_number: str) -> JsonObject:
        index = self.next_index
        self.next_index += 1
        return {"section_id": f"v10:{ancestor_number}.unmapped", "found_number": None,
                "found_heading": node.title, "match": "protocol_added", "source_indices": [index], "portion": "all"}


def merge_sources(source_lists) -> list[JsonObject]:
    merged: dict[str, JsonObject] = {}
    for sources in source_lists:
        for source in sources:
            existing = merged.get(source["section_id"])
            if existing is None:
                merged[source["section_id"]] = copy.deepcopy(source)
            else:
                existing["source_indices"] = sorted(set(existing["source_indices"] + source["source_indices"]))
    return list(merged.values())


def find_section(skeleton: JsonObject, number: str) -> JsonObject | None:
    parts = number.split(".")
    node = skeleton["sections"].get(parts[0])
    for depth in range(2, len(parts) + 1):
        if node is None:
            return None
        node = node.get("subsections", {}).get(".".join(parts[:depth])) if isinstance(node.get("subsections"), dict) else None
    return node


def allowed_statuses(schema: JsonObject, number: str) -> list[str]:
    definition = schema["$defs"].get("Section_" + number.replace(".", "_"), {})
    return definition.get("properties", {}).get("status", {}).get("enum", [])


def place_node(protocol: JsonObject, schema: JsonObject, placer: Placer, node, template_ancestor: JsonObject | None) -> None:
    """Fill the skeleton section for a template node; keep other nodes as additional subsections of the nearest template ancestor."""
    section = find_section(protocol, node.number) if node.from_template else None
    if section is None:
        if template_ancestor is None:
            return
        template_ancestor["additional_subsections"].append(
            placer.additional_subsection(node, template_ancestor["number"]))
        return
    if node.blocks:
        if "populated" in allowed_statuses(schema, node.number):
            blocks, sources = placer.section_content(node, node.number)
            section["status"] = "populated"
            section["content"] = blocks
            section["custody"] = custody("v10_derived", sources)
        else:
            held = copy.copy(node)
            held.from_template = False
            section["additional_subsections"].append(placer.additional_subsection(held, node.number))
    for child in node.children:
        place_node(protocol, schema, placer, child, section)


def place_document(document, pdf_path: Path, schema: JsonObject, crosswalk, outline, assets_dir: Path, template_file: str):
    """Return (protocol JSON, layout sidecar dict, restructure report)."""
    result = restructure(document, crosswalk, outline)
    protocol = minimal_instance(schema)
    protocol["source"] = {
        "path": str(pdf_path), "file_name": pdf_path.name, "sha256": file_sha256(pdf_path),
        "template": SOURCE_TEMPLATE, "extracted_at": now_iso(),
    }
    protocol["target"] = {"template": TARGET_TEMPLATE, "template_file": template_file}
    placer = Placer(assets_dir)
    for root in result.nodes:
        place_node(protocol, schema, placer, root, None)
    protocol["processing_log"] = [{
        "step": "place", "actor": copy.deepcopy(SCRIPT_ACTOR), "at": now_iso(),
        "summary": f"placed {placer.next_index} blocks from {pdf_path.name}",
    }]
    return protocol, placer.layout, result.report
