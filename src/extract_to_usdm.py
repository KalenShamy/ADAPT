"""Extract protocol PDFs to USDM v4 JSON and portable PNG assets.

The document-version extension stores: schema_version, assets, title_values,
title_others, sections (page/front/heading by narrative id), landscape_pages,
source_checks, title_source_text, and notes. Block layout/redactions live in XHTML data attributes;
front sections use empty section numbers. No PDF is needed after extraction.
"""

import argparse
import json
import re
from pathlib import Path

import pymupdf
from simple_error_log.errors import Errors
from usdm4 import USDM4
from usdm4.api.extension import ExtensionAttribute
from usdm4.api.wrapper import Wrapper

from block_xhtml import blocks_to_xhtml, xhtml_to_blocks
from pdf_to_cpt_docx import (
    REDACTED_TITLES, Document, Figure, FrontHeading, Heading, PageMark, Paragraph, Table,
    body_font_size,
    COVER_HEADING, ELECTRONIC_SIGNATURE_VALUE, join_sponsor,
    classify_paragraphs, collapse_redactions, collect_title_values, join_across_pages,
    merge_split_tables, read_pdf, registry_rows, structure, canonical_label,
)
from restructure import build_tree, front_parts

SIDECAR_URL = "urn:protocol-reformatter:pdf-layout:v1"
SYSTEM_VERSION = "1"
BOOKMARK_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)(?:\.?\s|$)")
UNDECODED_PHASE_CODE = "C48660"
REGISTRY_SCOPES = {"IND": "fda-ind", "NCT": "nct", "WHO": "who", "EU CT": "ema",
                   "EU TRIAL NUMBER": "ema", "EUDRACT": "ema", "CTIS": "ema"}
TITLE_STOP = re.compile(r"^(Confidential:|Confidential$|Any and all information|This protocol was approved|Based on TMF|©)", re.IGNORECASE)
RUNNING_HEADER_VERSION = re.compile(r"^Version number:\s*(\S+)$", re.IGNORECASE)
TITLE_EMPTY_LABELS = re.compile(r"^(Clinical Scientist|Medical\s*/?Study Director|Medical/Study Director|Study Medical Director/Medical Monitor|Monitoring team|Other:|Pediatric investigational plan|Sponsor Signatory \().*", re.IGNORECASE)


def enrich_title(path, values, others, notes):
    with pymupdf.open(path) as pdf:
        lines = [line.strip() for line in pdf[0].get_text().splitlines() if line.strip()]
        second_page_lines = [line.strip() for line in pdf[1].get_text().splitlines()] if len(pdf) > 1 else []
    running_version = next((match.group(1) for match in map(RUNNING_HEADER_VERSION.match, second_page_lines) if match), None)
    if running_version:
        values.setdefault("version", running_version)
        notes.append("Version Number taken from the 'Version number:' running header on page 2")
    first_label = next((index for index, line in enumerate(lines)
                        if re.fullmatch(r"(?:AMENDED )?CLINICAL (?:TRIAL|STUDY) PROTOCOL(?: \d+)?", line, re.IGNORECASE)), 0)
    collected = {}
    current = None
    registry = registry_rows(" ".join(lines))
    date = next((line.partition(":")[2].strip() for line in lines[:first_label]
                 if line.startswith("Date:")), None)
    if date:
        values["date"] = date
    for line in lines[:first_label]:
        version = re.fullmatch(r"Protocol Version (\S+) Final", line, re.IGNORECASE)
        amendment = re.fullmatch(r"Protocol Amendment (\S+) Final", line, re.IGNORECASE)
        if version:
            values.setdefault("version", version.group(1))
        if amendment:
            values.setdefault("amendment_number", amendment.group(1))
    index = first_label
    while index < len(lines):
        line = lines[index]
        index += 1
        if TITLE_STOP.match(line):
            break
        label, separator, remainder = line.partition(":")
        key = canonical_label(label)
        if not key and index < len(lines):
            combined = line + " " + lines[index]
            combined_label, combined_separator, combined_remainder = combined.partition(":")
            key = canonical_label(combined_label)
            if key:
                label, separator, remainder = combined_label, combined_separator, combined_remainder
                index += 1
        if key:
            current = key
            collected.setdefault(key, [])
            if separator and remainder:
                collected[key].append(remainder.strip())
            continue
        if TITLE_EMPTY_LABELS.match(line) or (line.endswith(":") and not canonical_label(line)):
            current = None
        elif current and line:
            collected[current].append(line)
    for key, pieces in collected.items():
        value = " ".join(pieces).strip()
        if key not in ("registry_id", "regulatory_ids", "sponsor_address", "medical_monitor", "signatory",
                       "primary_intervention") \
                and value and not ELECTRONIC_SIGNATURE_VALUE.match(value):
            values[key] = value
    if collected.get("sponsor_address"):
        sponsor = ""
        for piece in collected.get("sponsor", []) + collected["sponsor_address"]:
            sponsor = join_sponsor(sponsor, piece)
        values["sponsor"] = sponsor
    if registry:
        values["regulatory_ids"] = "; ".join(f"{label}: {identifier}" for label, identifier in dict.fromkeys(registry))
    if not values.get("sponsor"):
        company = next((line.strip() for line in lines if line.strip() == "Regeneron Pharmaceuticals, Inc."), None)
        if company:
            values["sponsor"] = company
            notes.append("Sponsor recovered from the repeated company line in the Regeneron title-page layout")
    if not values.get("title"):
        compound_index = next((index for index, line in enumerate(lines) if canonical_label(line) == "compound"), None)
        if compound_index is not None:
            title_start = next((index + 1 for index in range(compound_index - 1, -1, -1)
                                if lines[index].lower() == "clinical study protocol"), None)
            title_lines = lines[title_start:compound_index] if title_start is not None else []
            if title_lines and all(line.upper() == line for line in title_lines):
                values["title"] = " ".join(title_lines)
                notes.append("Full Title recovered from the uppercase title between Clinical Study Protocol and Compound")
    amendment = re.search(r"\bAmendment\s+\d+(?:\s+EU-\d+)?", values.get("version", ""), re.IGNORECASE)
    if amendment and not values.get("amendment_number"):
        values["amendment_number"] = amendment.group()
    notes.append("Title fields supplemented from explicit first-page labels; unrecognized title fields retained verbatim")


def finish_title(values, others, notes):
    """Fold fallback fields into their v011 slots and drop extras that repeat a mapped value."""
    primary_intervention = values.pop("primary_intervention", None)
    if primary_intervention and not values.get("compound"):
        values["compound"] = primary_intervention
        notes.append("Investigational Product Code(s) taken from 'Primary study intervention(s)'")
    mapped_text = " ".join(values.values())
    others[:] = [(label, value) for label, value in others
                 if label or not (COVER_HEADING.match(value) or value in mapped_text)]


def numbered_bookmarks(path):
    """(number, title, page) for each PDF bookmark that starts with a section number."""
    with pymupdf.open(path) as pdf:
        bookmarks = pdf.get_toc()
    rows = []
    for _, text, page in bookmarks:
        match = BOOKMARK_NUMBER.match(text)
        if match:
            rows.append((match.group(1), text[match.end():].strip(), page))
    return rows


def load_source(path):
    items, toc_pages = read_pdf(str(path))
    document = structure(items, toc_pages, body_font_size(items), numbered_bookmarks(path))
    for part in ("front", "body"):
        blocks = collapse_redactions(merge_split_tables(join_across_pages(getattr(document, part))))
        classify_paragraphs(blocks)
        setattr(document, part, blocks)
    return document


def paragraphs(blocks):
    for block in blocks:
        if isinstance(block, Paragraph):
            yield block
        elif isinstance(block, Table):
            for row in block.grid:
                for cell in row:
                    if cell:
                        yield from cell["paragraphs"]


def synopsis_title(document, values, notes):
    if values.get("title"):
        return
    in_synopsis = False
    pending = False
    for block in document.body:
        if isinstance(block, Heading):
            in_synopsis = block.number == "1.1"
        if not in_synopsis:
            continue
        for paragraph in paragraphs([block]):
            text = paragraph.text.strip()
            if pending and text:
                values["title"] = text
                notes.append("Full Title recovered from synopsis Protocol Title before synopsis filtering")
                return
            label, separator, remainder = text.partition(":")
            if canonical_label(label) == "title":
                if separator and remainder.strip():
                    values["title"] = remainder.strip()
                    notes.append("Full Title recovered from synopsis Protocol Title before synopsis filtering")
                    return
                pending = True


def identification(values, others):
    identifiers = []
    seen = set()
    registry_text = " ".join([values.get("regulatory_ids", "")] + [f"{label or ''} {text}" for label, text in others])
    for label, identifier in registry_rows(registry_text):
        if (label, identifier) in seen:
            continue
        seen.add((label, identifier))
        scope = ({"standard": REGISTRY_SCOPES[label]} if label in REGISTRY_SCOPES else
                 {"non_standard": {"type": "registry", "name": label}})
        identifiers.append({"identifier": identifier, "scope": scope})
    if values.get("protocol_number"):
        identifiers.append({"identifier": values["protocol_number"], "scope": {"non_standard": {
            "type": "pharma", "role": "sponsor", "name": values.get("sponsor") or "Source sponsor not stated"}}})
    roles = {"sponsor": {"name": values["sponsor"]}} if values.get("sponsor") else {}
    return {"titles": {"official": values.get("title", ""), "brief": values.get("short_title", ""),
                        "acronym": values.get("acronym", "")}, "identifiers": identifiers, "roles": roles}


def same_heading_title(detected, bookmarked):
    if detected in REDACTED_TITLES and bookmarked in REDACTED_TITLES:
        return True
    return re.sub(r"\W+", "", detected).casefold() == re.sub(r"\W+", "", bookmarked).casefold()


def source_checks(path, document):
    headings = [block for block in document.body if isinstance(block, Heading)]
    scanned_pages = sorted(block.page for block in document.body + document.front
                           if isinstance(block, Figure) and block.full_page)
    detected = {heading.number: heading for heading in headings}
    mismatches, unreadable = [], []
    for number, title, page in numbered_bookmarks(path):
        heading = detected.get(number)
        label = f"bookmark {number} {title} p{page}"
        if page in scanned_pages:
            unreadable.append(f"{label}: on scanned page, not checkable")
        elif not heading or heading.page != page:
            mismatches.append(f"{label}: detected " + (f"p{heading.page}" if heading else "not found"))
        elif not same_heading_title(heading.title, title):
            mismatches.append(f"{label}: detected title {heading.title!r}")
    with pymupdf.open(path) as pdf:
        bookmark_count = len(pdf.get_toc())
    return {"bookmarks": bookmark_count, "bookmark_mismatches": mismatches,
            "bookmarks_on_scanned_pages": unreadable, "scanned_pages": scanned_pages}


def extract_file(pdf_path, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    document = load_source(pdf_path)
    notes = []
    values, others = collect_title_values(document, notes)
    synopsis_title(document, values, notes)
    enrich_title(pdf_path, values, others, notes)
    finish_title(values, others, notes)
    assets = out_dir / f"{pdf_path.stem}.usdm.assets"
    sections, metadata = [], []

    def add_section(number, title, page, blocks, front=False, heading=True):
        xhtml = blocks_to_xhtml(blocks, assets)
        restored = xhtml_to_blocks(xhtml, assets, page)
        if restored != blocks:
            raise ValueError(f"Lossy XHTML round trip: {pdf_path.name} section {number or title}")
        sections.append({"section_number": number, "section_title": title, "text": xhtml})
        metadata.append({"page": page, "front": front, "heading": heading})

    for heading, blocks in front_parts(document.front):
        add_section("", heading.title if heading else "", heading.page if heading else 0,
                    blocks, front=True, heading=heading is not None)

    def add_tree(nodes):
        for node in nodes:
            add_section(node.number, node.title, node.page, node.blocks)
            add_tree(node.children)

    tree = build_tree(document.body)
    if not tree and document.body:
        raise ValueError(f"No numbered sections found in {pdf_path}")
    add_tree(tree)
    errors = Errors()
    facade = USDM4()
    assembler = facade.assembler(errors)
    assembler.execute({
        "identification": identification(values, others),
        "document": {"document": {"label": pdf_path.stem, "version": values.get("version", ""),
                                   "template": "TransCelerate CPT v010", "version_date": values.get("date", "")}, "sections": sections},
        "population": {"label": "Unspecified population"},
        "study_design": {"label": "Unspecified design", "trial_phase": values.get("phase", "")},
        "study": {"name": {"identifier": values.get("protocol_number") or pdf_path.stem},
                  "version": values.get("version", "")},
    })
    wrapper = assembler.wrapper("protocol-reformatter", SYSTEM_VERSION)
    messages = errors.to_dict(level=Errors.DEBUG)
    for entry in messages:
        print(f"{pdf_path.name}: {entry['level']}: {entry['message']}")
    if wrapper is None or any(entry["level"] == "Error" for entry in messages):
        raise ValueError(f"USDM assembly failed for {pdf_path}")
    # The assembler requires a design input and invents default sex/model/phase.
    # USDM permits no designs: do not publish these defaults as extracted facts.
    phase = wrapper.study.versions[0].studyDesigns[0].studyPhase if values.get("phase") else None
    wrapper.study.versions[0].studyDesigns = []
    notes.append("Structured design/population omitted: assembler defaults are not source facts; phase retained verbatim in title_values")
    notes.append("DDF00187: the required loss-free XHTML data-* layout attributes are outside the restricted USDM-XHTML schema; these validation failures are retained, not suppressed")
    notes.append("USDM document status uses the assembler's Draft default when no structured status is supplied; it is not an extracted status")
    version = wrapper.study.documentedBy[0].versions[0]
    if phase and phase.standardCode.code != UNDECODED_PHASE_CODE:
        version.extensionAttributes.append(ExtensionAttribute.model_validate({
            "id": "ExtractedTrialPhase", "url": "urn:protocol-reformatter:trial-phase",
            "valueAliasCode": phase.model_dump(), "instanceType": "ExtensionAttribute"}))
    if len(version.contents) != len(metadata):
        raise ValueError("Assembler lost narrative sections")
    with pymupdf.open(pdf_path) as pdf:
        title_source_text = pdf[0].get_text()
    sidecar = {"schema_version": 1, "assets": assets.name, "title_values": values, "title_others": others,
               "title_source_text": title_source_text,
               "sections": {content.id: info for content, info in zip(version.contents, metadata)},
               "landscape_pages": {block.page: block.landscape for block in document.body + document.front
                                   if isinstance(block, PageMark)},
               "source_checks": source_checks(pdf_path, document),
               "notes": notes + [f"Assembler {entry['level']}: {entry['message']}" for entry in messages]}
    version.extensionAttributes.append(ExtensionAttribute(
        id="ProtocolReformatterLayout", url=SIDECAR_URL, valueString=json.dumps(sidecar), instanceType="ExtensionAttribute"))
    path = out_dir / f"{pdf_path.stem}.usdm.json"
    path.write_text(wrapper.to_json(), encoding="utf-8")
    validation = facade.validate(str(path))
    path.with_suffix(".validation.txt").write_text(validation.format_text(), encoding="utf-8")
    print(f"{pdf_path.name}: {len(sections)} sections; rules valid={validation.is_valid} -> {path}")
    return path


def load_document(path):
    wrapper = Wrapper.model_validate_json(path.read_text(encoding="utf-8"))
    version = wrapper.study.documentedBy[0].versions[0]
    extension = next(item for item in version.extensionAttributes if item.url == SIDECAR_URL)
    sidecar = json.loads(extension.valueString)
    if sidecar["schema_version"] != 1:
        raise ValueError("Unsupported layout extension version")
    assets = (path.parent / sidecar["assets"]).resolve()
    if not assets.is_relative_to(path.parent.resolve()):
        raise ValueError("Asset directory outside JSON directory")
    items = {item.id: item for item in wrapper.study.versions[0].narrativeContentItems}
    document = Document()
    for content in version.contents:
        info = sidecar["sections"][content.id]
        blocks = document.front if info["front"] else document.body
        if info["heading"]:
            blocks.append(FrontHeading(content.sectionTitle, info["page"]) if info["front"] else
                          Heading(content.sectionNumber, content.sectionTitle, info["page"]))
        blocks.extend(xhtml_to_blocks(items[content.contentItemId].text, assets, info["page"]))
    document.body.extend(PageMark(landscape, int(page)) for page, landscape in sidecar["landscape_pages"].items())
    return document, sidecar


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("pdfs", type=Path, nargs="+")
    args = parser.parse_args()
    for path in args.pdfs:
        extract_file(path, args.out_dir)


if __name__ == "__main__":
    main()
