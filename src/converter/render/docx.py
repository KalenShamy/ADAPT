"""Render protocol JSON (schema/protocol.schema.json) to a v011 docx on the CPT template."""

import re
from pathlib import Path

from docx.enum.text import WD_COLOR_INDEX
from docx.shared import RGBColor

from block_xhtml import xhtml_to_blocks
from converter.title_page import TITLE_PAGE_UNMAPPED_ID, render_values
from pdf_to_cpt_docx import Writer, write_block, write_title_page

type JsonObject = dict[str, object]

TEMPLATE_VERSION = "v011"
MISSING_TEXT = "[NOT EXTRACTED: no content found in the source protocol]"
NOT_APPLICABLE_TEXT = "Not applicable"
AI_NOTE = "[AI-drafted from the source protocol. Review required.]"
MISSING_COLOR = RGBColor(0xC0, 0x00, 0x00)
NUMBER_PART = re.compile(r"^[\d.]+")


class DocxRenderer:
    def __init__(self, template: Path, layout: dict[str, str], assets_dir: Path, protocol_label: str) -> None:
        self.writer = Writer(str(template), TEMPLATE_VERSION)
        self.layout = layout
        self.assets_dir = assets_dir
        self.protocol_label = protocol_label
        self.toc_entries: list[tuple[int, str, str]] = []
        self.protocol_number = ""

    def render(self, protocol: JsonObject, out_path: Path) -> None:
        writer = self.writer
        self.title_page(protocol)
        self.unplaced_front(protocol)
        writer.toc()
        writer.page_break()
        sections = protocol["sections"]
        for key in sorted(sections, key=int):
            self.section(sections[key], level=1)
        writer.finish_sections()
        writer.fill_toc(self.toc_entries)
        writer.footers(self.protocol_label or self.protocol_number)
        writer.save(str(out_path))

    def title_page(self, protocol: JsonObject) -> None:
        others = [
            (item["v10_source"]["found_heading"], item["blocks"][0]["text"])
            for item in protocol.get("unplaced_v10_content", []) if is_title_field(item)
        ]
        values = render_values(protocol["title_page"])
        self.protocol_number = write_title_page(self.writer, values, others, []) or ""
        self.writer.page_break()

    def unplaced_front(self, protocol: JsonObject) -> None:
        """Front matter has no v011 slot yet; keep it ahead of the TOC as the old converter did."""
        items = [item for item in protocol.get("unplaced_v10_content", []) if not is_title_field(item)]
        for item in items:
            for block in item["blocks"]:
                self.block(block)
        if items:
            self.writer.page_break()

    def section(self, section: JsonObject, level: int) -> None:
        status = section["status"]
        if status == "omitted":
            return
        number = section.get("number") or ""
        self.heading(str(number), str(section["title"]), level)
        self.status_text(status, section.get("content", []))
        for block in section.get("content", []):
            self.block(block)
        children = section.get("subsections", {})
        for key in sorted(children, key=natural_key):
            self.section(children[key], level + 1)
        for extra in section.get("additional_subsections", []):
            self.additional(extra, level + 1)

    def additional(self, node: JsonObject, level: int) -> None:
        self.heading("", str(node["title"]), level)
        for block in node["content"]:
            self.block(block)
        for child in node["subsections"]:
            self.additional(child, level + 1)

    def heading(self, number: str, title: str, level: int) -> None:
        self.toc_entries.append((level, number, title))
        self.writer.numbered_heading(title, level)

    def status_text(self, status: str, content: list[JsonObject]) -> None:
        if status == "not_applicable":
            self.writer.paragraph(self.writer.styles["body"], text=NOT_APPLICABLE_TEXT)
        elif status == "missing":
            paragraph = self.writer.paragraph(self.writer.styles["body"], text=MISSING_TEXT)
            for run in paragraph.runs:
                run.font.color.rgb = MISSING_COLOR
        elif any(is_ai(block) for block in content):
            self.highlighted(AI_NOTE)

    def highlighted(self, text: str) -> None:
        paragraph = self.writer.paragraph(self.writer.styles["body"], text=text)
        for run in paragraph.runs:
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW

    def block(self, block: JsonObject) -> None:
        if is_ai(block):
            self.ai_block(block)
            return
        xhtml = self.layout.get(str(block.get("source_index")))
        if xhtml is not None:
            for rich in xhtml_to_blocks(xhtml, self.assets_dir):
                write_block(self.writer, rich)
        else:
            self.plain_block(block)

    def plain_block(self, block: JsonObject) -> None:
        if block["type"] == "table":
            self.writer.simple_table(block["rows"])
        elif block["type"] == "heading":
            self.writer.paragraph(self.writer.styles["field_label"], text=block["text"])
        else:
            self.writer.paragraph(self.writer.styles["body"], text=block["text"])

    def ai_block(self, block: JsonObject) -> None:
        self.plain_block(block)
        paragraph = self.writer.doc.paragraphs[-1] if block["type"] != "table" else None
        for run in paragraph.runs if paragraph is not None else []:
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def is_title_field(item: JsonObject) -> bool:
    return item["v10_source"]["section_id"] == TITLE_PAGE_UNMAPPED_ID


def is_ai(block: JsonObject) -> bool:
    return block["custody"]["origin"] in ("ai_generated", "ai_transformed")


def natural_key(key: str) -> tuple[int, ...]:
    match = NUMBER_PART.match(key)
    return tuple(int(part) for part in match.group(0).split(".") if part) if match else ()


def render_docx(protocol: JsonObject, layout: dict[str, str], assets_dir: Path,
                template: Path, out_path: Path, protocol_label: str = "") -> None:
    DocxRenderer(template, layout, assets_dir, protocol_label).render(protocol, out_path)
