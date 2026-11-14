"""Extract the title page of a CPT v10 protocol and render it with the v11 title page template.

Usage: python convert_v10_title_page.py <v10 protocol.docx> [--output <v11 title page.docx>]
"""

import argparse
import re
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

from docx import Document
from docx.enum.text import WD_COLOR_INDEX
from docx.text.run import Run
from lxml.etree import _Element

from render_v11_title_page import FIELD_LABELS, render_title_page

WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
TRACKED_DELETION_TAGS = {f"{WORD_NS}del", f"{WORD_NS}moveFrom"}

TITLE_PAGE_START = "Title Page"
TITLE_PAGE_END_MARKERS = ("Protocol Amendment Summary of Changes", "Table of Contents")

V10_LABELS = (
    "Protocol Title",
    "Protocol Number",
    "Protocol Version",
    "Amendment Number",
    "Amendment Scope",
    "Compound",
    "Brief Title",
    "Study Phase",
    "Acronym",
    "Sponsor Name",
    "Legal Registered Address",
    "Manufacturer",
    "Regulatory Agency Identifier Number(s)",
    "Approval Date",
    "Sponsor Signatory",
    "Medical Monitor Name and Contact Information",
)
LABEL_PATTERN = re.compile(
    r"^\s*(" + "|".join(re.escape(label) for label in V10_LABELS) + r")\s*:\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
CANONICAL_LABELS = {label.lower(): label for label in V10_LABELS}

# v10 fields the current v11 template has no value slot for. Reported so they don't vanish silently.
LABELS_WITHOUT_V11_SLOT = ("Protocol Title", "Brief Title", "Acronym", "Approval Date")

MEDICAL_MONITOR_LABEL = "Medical Monitor Name and Contact Information"
ORIGINAL_PROTOCOL_VERSIONS = {"1", "1.0"}
SIGNATORY_DATE_HEADER = "date"

Table = list[list[str]]


@dataclass
class V10TitlePage:
    fields: dict[str, str] = field(default_factory=dict)
    tables: dict[str, list[Table]] = field(default_factory=dict)

    def get(self, label: str) -> str:
        return self.fields.get(label, "")


def visible_text(element: _Element) -> str:
    """Text as it reads with all tracked changes accepted."""
    parts = []
    for node in element.iter(f"{WORD_NS}t", f"{WORD_NS}tab", f"{WORD_NS}br"):
        if any(ancestor.tag in TRACKED_DELETION_TAGS for ancestor in node.iterancestors()):
            continue
        if node.tag == f"{WORD_NS}t":
            parts.append(node.text or "")
        elif node.tag == f"{WORD_NS}tab":
            parts.append("\t")
        else:
            parts.append("\n")
    return "".join(parts)


def table_rows(table: _Element) -> Table:
    rows = []
    for row in table.iter(f"{WORD_NS}tr"):
        cells = []
        for cell in row.iter(f"{WORD_NS}tc"):
            paragraphs = [visible_text(p).strip() for p in cell.iter(f"{WORD_NS}p")]
            cells.append("\n".join(p for p in paragraphs if p))
        if any(cells):
            rows.append(cells)
    return rows


def title_page_blocks(document_path: Path) -> list[str | Table]:
    body = Document(str(document_path)).element.body
    blocks: list[str | Table] = []
    for child in body:
        if child.tag == f"{WORD_NS}p":
            text = visible_text(child).strip()
            if text.startswith(TITLE_PAGE_END_MARKERS):
                break
            if text == TITLE_PAGE_START:
                blocks.clear()
            elif text:
                blocks.append(text)
        elif child.tag == f"{WORD_NS}tbl":
            blocks.append(table_rows(child))
        elif child.tag == f"{WORD_NS}sdt":
            break
    return blocks


def collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def parse_title_page(blocks: list[str | Table]) -> V10TitlePage:
    title_page = V10TitlePage()
    current_label: str | None = None
    for block in blocks:
        if isinstance(block, list):
            if current_label:
                title_page.tables.setdefault(current_label, []).append(block)
            current_label = None
            continue

        match = LABEL_PATTERN.match(block)
        if match:
            current_label = CANONICAL_LABELS[match.group(1).lower()]
            title_page.fields[current_label] = collapse_whitespace(match.group(2))
        elif current_label:
            existing = title_page.fields[current_label]
            title_page.fields[current_label] = collapse_whitespace(f"{existing} {block}")
        elif "medical monitor" in block.lower():
            # Protocols often replace the labelled field with a sentence pointing elsewhere.
            title_page.fields[MEDICAL_MONITOR_LABEL] = block
    return title_page


def placeholder(name: str) -> str:
    return f"[{name}]"


def as_trial_phase(study_phase: str) -> str:
    if not study_phase:
        return placeholder("Trial Phase")
    if study_phase.lower().startswith("phase"):
        return study_phase
    return f"Phase {study_phase}"


def is_original_protocol(title_page: V10TitlePage) -> str:
    if title_page.get("Amendment Number"):
        return "No"
    version = title_page.get("Protocol Version")
    if not version:
        return placeholder("Yes/No")
    return "Yes" if version in ORIGINAL_PROTOCOL_VERSIONS else "No"


def regulatory_identifiers(title_page: V10TitlePage) -> list[tuple[str, str]]:
    identifiers = []
    for table in title_page.tables.get("Regulatory Agency Identifier Number(s)", []):
        for row in table:
            if len(row) >= 2 and row[1]:
                identifiers.append((row[0].rstrip(": "), row[1]))
    return identifiers


def sponsor_signatory(title_page: V10TitlePage) -> tuple[str, str, str]:
    tables = title_page.tables.get("Sponsor Signatory", [])
    if not tables:
        return placeholder("Name"), placeholder("Title"), placeholder("Date")

    first_row = tables[0][0]
    name, _, title = first_row[0].partition("\n")
    date_cell = first_row[-1]
    # The v10 table cell often keeps the "Date" header, with the real date typed above the table.
    if not date_cell or date_cell.lower() == SIGNATORY_DATE_HEADER:
        date_cell = title_page.get("Sponsor Signatory") or title_page.get("Approval Date")
    return name, " ".join(title.split()), date_cell or placeholder("Date")


def build_v11_context(title_page: V10TitlePage) -> dict[str, str]:
    compound = title_page.get("Compound")
    trial_phase = as_trial_phase(title_page.get("Study Phase"))
    identifiers = regulatory_identifiers(title_page)
    agency, id_number = identifiers[0] if identifiers else (placeholder("Agency"), placeholder("ID Number"))
    signatory_name, signatory_title, signatory_date = sponsor_signatory(title_page)
    original = is_original_protocol(title_page)
    not_applicable_if_original = "Not applicable" if original == "Yes" else None

    return FIELD_LABELS | {
        # Full title and short title. v10 has a free-text title, so these M11 terms need filling in by hand.
        "intervention_model": placeholder("intervention model"),
        "primary_purpose": placeholder("primary purpose"),
        "trial_phase": trial_phase,
        "blinding": placeholder("blinding"),
        "health_measurement_outcome": placeholder("health measurement/outcome"),
        "investigational_intervention": compound or placeholder("investigational intervention"),
        "intervention_form": placeholder("intervention form"),
        "condition_disease": placeholder("condition/disease"),
        "male_and_or_female": placeholder("male and/or female"),
        "X_to_X_years_of_age": placeholder("X to X years of age"),
        # The template uses {{ number }} for both the number of arms and the product code.
        "number": compound or placeholder("number"),
        # Identifiers
        "Trial_phase": trial_phase,
        "protocol_number": title_page.get("Protocol Number") or placeholder("protocol number"),
        "Version_Number": title_page.get("Protocol Version") or placeholder("Version Number"),
        "Version_Date": placeholder("Version Date"),
        "yes_no": original,
        "amendment_identifier": title_page.get("Amendment Number")
        or not_applicable_if_original
        or placeholder("amendment identifier"),
        "Amendment_Scope_Type": title_page.get("Amendment Scope")
        or not_applicable_if_original
        or placeholder("Amendment Scope"),
        "Country_Identifier": placeholder("Country Identifier"),
        "Region_Identifier": placeholder("Region Identifier"),
        "Enter_Site_Identifier": placeholder("Site Identifier"),
        "name": placeholder("name"),
        "insert_device_manufacturer_and_address": title_page.get("Manufacturer")
        or placeholder("insert device manufacturer and address"),
        # Sponsor
        "Sponsor_Co_sponsor_Name": title_page.get("Sponsor Name") or placeholder("Sponsor/Co-sponsor Name"),
        "Sponsor_Co_sponsor_Legal_Address": title_page.get("Legal Registered Address")
        or placeholder("Sponsor/Co-sponsor Legal Address"),
        "Local_Sponsor_Name": placeholder("Local Sponsor Name"),
        "Local_Sponsor_Legal_Address": placeholder("Local Sponsor Legal Address"),
        # Regulatory identifier table
        "Agency": agency,
        "ID_Number": id_number,
        # Signatory table
        "Name": signatory_name,
        "Title": signatory_title,
        "Date": signatory_date,
        "will_be_provided_separately_OR_can_be_found_in_XX": title_page.get(MEDICAL_MONITOR_LABEL)
        or placeholder("will be provided separately OR can be found in XX"),
    }


def is_placeholder(value: str) -> bool:
    return value.startswith("[") and value.endswith("]")


def highlight_placeholders(docx_path: Path, placeholders: set[str]) -> None:
    """Highlight red every placeholder the converter inserted, splitting runs so only the placeholder is marked."""
    if not placeholders:
        return
    # Longest first, so "[name]" doesn't match inside a longer placeholder.
    placeholder_pattern = re.compile(
        "(" + "|".join(re.escape(text) for text in sorted(placeholders, key=len, reverse=True)) + ")"
    )
    document = Document(str(docx_path))
    for run_element in list(document.element.body.iter(f"{WORD_NS}r")):
        run_text = Run(run_element, document).text
        if not placeholder_pattern.search(run_text):
            continue
        for segment in placeholder_pattern.split(run_text):
            if not segment:
                continue
            segment_run = Run(deepcopy(run_element), document)
            segment_run.text = segment
            if segment in placeholders:
                segment_run.font.highlight_color = WD_COLOR_INDEX.RED
            run_element.addprevious(segment_run._r)
        run_element.getparent().remove(run_element)
    document.save(str(docx_path))


def report_dropped_content(title_page: V10TitlePage) -> None:
    unplaced = [label for label in LABELS_WITHOUT_V11_SLOT if title_page.get(label)]
    if unplaced:
        print(f"No v11 template slot for: {', '.join(unplaced)}")
    extra_identifiers = regulatory_identifiers(title_page)[1:]
    if extra_identifiers:
        print(f"Only the first regulatory identifier fits the template; dropped {len(extra_identifiers)} more")


def v11_file_name(v10_protocol: Path) -> str:
    """test1_v10.docx -> test1_v11.docx; other names get _v11 appended."""
    if "v10" in v10_protocol.stem:
        return f"{v10_protocol.stem.replace('v10', 'v11')}.docx"
    return f"{v10_protocol.stem}_v11.docx"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("v10_protocol", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    output_path = args.output or Path(__file__).resolve().parent / v11_file_name(args.v10_protocol)
    title_page = parse_title_page(title_page_blocks(args.v10_protocol))
    context = build_v11_context(title_page)
    render_title_page(context, output_path, remove_instructions=True)
    report_dropped_content(title_page)

    placeholders_left = {key: value for key, value in context.items() if is_placeholder(value)}
    highlight_placeholders(output_path, set(placeholders_left.values()))
    print(f"Placeholders left to fill (highlighted red): {', '.join(sorted(placeholders_left))}")


if __name__ == "__main__":
    main()
