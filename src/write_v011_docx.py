"""Write v011 CPT documents from extracted USDM JSON, without opening a PDF."""

import argparse
import json
from collections import Counter
from pathlib import Path

from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from usdm4 import USDM4
from cross_references import rewrite_cross_references, rewrite_heading_titles, rewrite_title_values
from extract_to_usdm import load_document
from pdf_to_cpt_docx import Figure, FrontHeading, Heading, Paragraph, Redaction, Subheading, Table, Writer, write_block, write_title_page
from restructure import iter_blocks, iter_nodes, load_crosswalk, restructure, template_outline

RESOURCES = Path(__file__).resolve().parent.parent / "resources"
DEFAULT_TEMPLATE = RESOURCES / "CPT_CoreBWE-v011.docx"
DEFAULT_CROSSWALK = RESOURCES / "research" / "crosswalk.tsv"
VERSION = "v011"
AMENDMENT_DETAILS_TITLE = "Amendment Details"
BOOKMARK_ID_START = 100000


def block_texts(block):
    if isinstance(block, Paragraph):
        return ["".join(span.text for span in block.spans).replace("\t", " ")]
    if isinstance(block, Table):
        return [text for row in block.grid for cell in row if cell
                for paragraph in cell["paragraphs"] for text in block_texts(paragraph)]
    if isinstance(block, (Subheading, FrontHeading)):
        return [block.title]
    if isinstance(block, Redaction):
        return ["[Redacted: CCI]"]
    return []


def table_provenance(source_blocks, output_blocks):
    sources = []
    heading = "front"
    for block in source_blocks:
        if isinstance(block, Heading):
            heading = block.number
        if isinstance(block, Table):
            sources.append((block, heading, {id(paragraph) for row in block.grid for cell in row if cell
                                             for paragraph in cell["paragraphs"]}))
    outputs = []
    for block in output_blocks:
        if not isinstance(block, Table):
            continue
        paragraphs = {id(paragraph) for row in block.grid for cell in row if cell for paragraph in cell["paragraphs"]}
        outputs.append([index for index, (source, heading, source_paragraphs) in enumerate(sources)
                        if source is block or paragraphs & source_paragraphs])
    return {"sources": [{"page": source.first_page, "section": heading} for source, heading, paragraphs in sources],
            "outputs": outputs}


def write_audited_block(writer, block):
    index = len(writer.block_audit)
    bookmark_id = str(BOOKMARK_ID_START + index)
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), bookmark_id)
    start.set(qn("w:name"), f"source_block_{index}")
    writer.final_sect.addprevious(start)
    write_block(writer, block)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), bookmark_id)
    writer.final_sect.addprevious(end)
    writer.block_audit.append({"id": bookmark_id, "type": type(block).__name__,
                               "text": " ".join(block_texts(block)).split(),
                               "tables": int(isinstance(block, Table)),
                               "figures": int(isinstance(block, Figure))})


def block_page(block):
    return getattr(block, "first_page", None) or getattr(block, "page", 0)


class OrientationTracker:
    def __init__(self, writer, landscape_pages):
        self.writer = writer
        self.landscape_pages = landscape_pages

    def apply(self, block):
        page = block_page(block)
        if page in self.landscape_pages:
            self.writer.set_orientation(self.landscape_pages[page])


def write_nodes(writer, nodes, orientation, toc_entries, warnings):
    for node in nodes:
        if node.blocks:
            orientation.apply(node.blocks[0])
        toc_entries.append((node.level, node.number, node.title))
        writer.numbered_heading(node.title, node.level)
        for block in node.blocks:
            orientation.apply(block)
            write_audited_block(writer, block)
        write_nodes(writer, node.children, orientation, toc_entries, warnings)


def count_words(blocks):
    total = 0
    for block in blocks:
        if isinstance(block, Paragraph):
            total += len(block.text.split())
        elif isinstance(block, Table):
            for row in block.grid:
                for cell in row:
                    if cell:
                        total += sum(len(paragraph.text.split()) for paragraph in cell["paragraphs"])
    return total


def section(lines, title, items):
    if items:
        lines.append(f"\n{title} ({len(items)})")
        lines.extend(f"  {item}" for item in items)


def build_report(name, result, crossrefs, title_notes, counts):
    report = result.report
    lines = [f"Report for {name}", "", counts]
    section(lines, "Source sections without a crosswalk row (placement shown)", report.unmatched)
    section(lines, "Split rows needing manual review", report.split_review)
    section(lines, "v011 sections left with the placeholder", report.placeholders)
    section(lines, "Synopsis fields dropped", report.synopsis_dropped)
    section(lines, "Objectives", report.objectives)
    section(lines, "Title page", title_notes)
    section(lines, "Notes", report.notes)
    lines.append(f"\nCross-references rewritten: {crossrefs.rewritten}")
    section(lines, "Cross-references to review", crossrefs.review)
    section(lines, "Cross-references not found in output", crossrefs.unresolved)
    return "\n".join(lines) + "\n"


def write_file(json_path, out_dir, template=DEFAULT_TEMPLATE, crosswalk=None, outline=None):
    out_dir.mkdir(parents=True, exist_ok=True)
    name = json_path.name.removesuffix(".usdm.json")
    document, sidecar = load_document(json_path)
    source_blocks = document.front + document.body
    source_counts = {"words": count_words(source_blocks),
                     "tables": sum(isinstance(block, Table) for block in source_blocks),
                     "figures": sum(isinstance(block, Figure) for block in source_blocks)}
    source_texts = Counter(text for block in source_blocks if isinstance(block, (Paragraph, Table))
                           for text in block_texts(block) if text.strip())
    synopsis_texts = Counter()
    heading = "front"
    for block in source_blocks:
        if isinstance(block, Heading):
            heading = block.number
        if heading == "1.1":
            synopsis_texts.update(text for text in block_texts(block) if text.strip())
    result = restructure(document, crosswalk or load_crosswalk(DEFAULT_CROSSWALK), outline or template_outline(template))
    output_texts = Counter(text for block in iter_blocks(result) if isinstance(block, (Paragraph, Table))
                           for text in block_texts(block) if text.strip())
    narrative_changes = {"removed_or_relabelled": list((source_texts - output_texts).elements()),
                         "added_or_repeated": list((output_texts - source_texts).elements())}
    unexplained_removals = list((source_texts - output_texts - synopsis_texts).elements())
    tables = table_provenance(source_blocks, list(iter_blocks(result)))
    crossrefs = rewrite_cross_references(list(iter_blocks(result)), result.number_map, result.also_sources)
    rewrite_heading_titles(iter_nodes(result.nodes), result.number_map, result.also_sources, crossrefs)
    writer = Writer(str(template), VERSION)
    writer.block_audit = []
    title_notes = list(sidecar["notes"])
    title_others = rewrite_title_values(sidecar["title_others"], result.number_map, result.also_sources, crossrefs)
    protocol_id = write_title_page(writer, sidecar["title_values"], title_others, title_notes)
    orientation = OrientationTracker(writer, result.landscape_pages)
    writer.page_break()
    for block in result.front:
        orientation.apply(block)
        write_audited_block(writer, block)
    if result.amendment_details:
        write_block(writer, Subheading(AMENDMENT_DETAILS_TITLE, "bold"))
        for block in result.amendment_details:
            orientation.apply(block)
            write_audited_block(writer, block)
    writer.set_orientation(False)
    writer.page_break()
    writer.toc()
    writer.page_break()
    toc_entries, warnings = [], []
    write_nodes(writer, result.nodes, orientation, toc_entries, warnings)
    writer.finish_sections()
    writer.fill_toc(toc_entries)
    writer.footers(protocol_id or "")
    out_path = out_dir / f"{name}.docx"
    writer.save(str(out_path))
    blocks = list(iter_blocks(result))
    written_counts = {"words": count_words(blocks), "tables": sum(isinstance(block, Table) for block in blocks),
                      "figures": sum(isinstance(block, Figure) for block in blocks)}
    counts = f"{len(toc_entries)} headings; source {source_counts}; written {written_counts}"
    checks = sidecar["source_checks"]
    title_notes.extend(checks["bookmark_mismatches"])
    title_notes.extend(checks.get("bookmarks_on_scanned_pages", []))
    title_notes.append(f"Scanned pages retained as images (no OCR): {checks['scanned_pages']}")
    report = build_report(name, result, crossrefs, title_notes + warnings, counts)
    report += "\nNarrative reconciliation before cross-reference rewriting\n"
    report += "Changes reflect synopsis filtering/relabeling, generated placeholders/subheadings, and repeated objectives table headers. Full text is listed for review, not silently ignored.\n"
    for label, texts in narrative_changes.items():
        section_lines = []
        section(section_lines, label, texts)
        report += "\n".join(section_lines) + "\n"
    report += "\nTable provenance (zero-based source table indices; multiple output pieces are intentional objectives splits)\n"
    report += json.dumps(tables, indent=2) + "\n"
    validation = USDM4().validate(str(json_path))
    report += "\n" + validation.format_text()
    (out_dir / f"{name}.report.txt").write_text(report, encoding="utf-8")
    audit = {"source": source_counts, "written": written_counts,
             "extra_headings": [{"number": node.number, "title": node.title} for node in iter_nodes(result.nodes)
                                if not node.from_template],
             "outline": toc_entries, "bookmark_mismatches": checks["bookmark_mismatches"],
             "synopsis_dropped": result.report.synopsis_dropped,
             "narrative_changes": narrative_changes, "unexplained_removals": unexplained_removals,
             "tables": tables, "blocks": writer.block_audit}
    (out_dir / f"{name}.audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(f"{json_path.name}: {counts} -> {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--crosswalk", type=Path, default=DEFAULT_CROSSWALK)
    parser.add_argument("jsons", type=Path, nargs="+")
    args = parser.parse_args()
    crosswalk, outline = load_crosswalk(args.crosswalk), template_outline(args.template)
    for path in args.jsons:
        write_file(path, args.out_dir, args.template, crosswalk, outline)


if __name__ == "__main__":
    main()
