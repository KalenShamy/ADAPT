"""Build the docxtpl template for v11 Section 1 (Protocol Summary) from the CPT v011 core template.

Keeps the v11 Section 1 headings, instructions and fixed text, and swaps every value slot for a tag:
{{r name }} for a short value, {{p name }} for content copied from the v10 protocol, and a
{%tr for %} row loop for the objectives table.

Usage: python build_v11_section1_template.py
"""

from collections.abc import Iterator
from copy import deepcopy

from docx import Document
from docx.document import Document as DocumentObject
from docx.oxml.ns import qn
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.text.paragraph import Paragraph

from render_v11_title_page import PROJECT_ROOT, remove_broken_fields

V11_SOURCE_PATH = PROJECT_ROOT / "resources" / "CPT_CoreBWE-v011.docx"
TEMPLATE_PATH = PROJECT_ROOT / "resources" / "CPT_CoreBWE-v011_Section1_docxtpl.docx"

SECTION_HEADING_STYLE = "01Heading 1"
SECTION_TITLE = "Protocol Summary"
FIELD_LABEL_STYLE = "Heading (No TOC)"
CONTENT_STYLE = "Normal"
EXAMPLE_START = "<Start of example text>"
EXAMPLE_END = "<End of example text>"

# Overall Design key-aspects table: (row, cell) -> variable. Cells count per <w:tc>, not per grid column.
DESIGN_TABLE_VALUE_CELLS = {
    (0, 1): "intervention",
    (0, 3): "population_type",
    (1, 1): "intervention_model",
    (1, 3): "population_diagnosis_or_condition",
    (2, 1): "control_type",
    (2, 3): "population_age",
    (3, 1): "control_description",
    (3, 3): "site_distribution_and_geographic_scope",
    (4, 1): "intervention_assignment_method",
    (5, 1): "stratification",
    (6, 1): "drug_device_combination_product",
    (6, 3): "adaptive_trial_design",
}
# "Master Protocol" is one merged cell holding both the label and its value paragraph.
MASTER_PROTOCOL_CELL = (4, 2)

# Paragraphs whose text starts with the key: replaced by (new text, style). None keeps the text.
FIELD_LINES = {
    "Key aspects of the trial design": (None, CONTENT_STYLE),
    "Number of Arms:": ("Number of Arms: {{r number_of_arms }}", CONTENT_STYLE),
    "Trial Blind Schema": ("Trial Blind Schema: {{r trial_blind_schema }}", CONTENT_STYLE),
    "The following roles indicated": (
        "The following roles indicated will not be made aware of the treatment group assignment "
        "during the trial: {{r blinded_roles }}",
        CONTENT_STYLE,
    ),
    "A [Target/Maximum]": (
        "A {{r participant_target_or_maximum }} of {{r participant_count }} participants "
        "will be {{r participant_assignment }}.",
        CONTENT_STYLE,
    ),
    "Total planned duration of trial intervention": (None, CONTENT_STYLE),
    "Total planned duration of trial participation": (None, CONTENT_STYLE),
    "Committees:": ("Committees:", FIELD_LABEL_STYLE),
    "Independent committee(s):": ("Independent committee(s): {{r independent_committees }}", CONTENT_STYLE),
    "[Other committee(s)": ("Other committee(s): {{r other_committees }}", CONTENT_STYLE),
}

# The "[X] [unit of time]" line that follows each duration label.
DURATION_VALUE_LINES = {
    "Total planned duration of trial intervention": "{{r intervention_duration }}",
    "Total planned duration of trial participation": "{{r participation_duration }}",
}

# New {{p }} paragraphs inserted after the paragraph whose text starts with the key.
CONTENT_SLOTS_AFTER = {
    "A {{r participant_target_or_maximum }}": "number_of_participants_details",
    "Rules/procedures for any dose changes": "duration_details",
    "Include notes as necessary to further explain the trial schema": "schema",
}


def find_paragraph(document: DocumentObject, starts_with: str, after: Paragraph | None = None) -> Paragraph:
    """First body paragraph starting with the text, optionally only those after another paragraph."""
    paragraphs = document.paragraphs
    if after is not None:
        start = next(i for i, p in enumerate(paragraphs) if p._p is after._p) + 1
        paragraphs = paragraphs[start:]
    for paragraph in paragraphs:
        if paragraph.text.strip().startswith(starts_with):
            return paragraph
    raise SystemExit(f"v11 template changed: no paragraph starting with {starts_with!r}")


def set_text(paragraph: Paragraph, text: str, style: str) -> None:
    """Replace the paragraph content with one run of plain text, keeping the first run's formatting."""
    first_run_properties = paragraph._p.find(f"{qn('w:r')}/{qn('w:rPr')}")
    for child in list(paragraph._p):
        if child.tag != qn("w:pPr"):
            paragraph._p.remove(child)
    run = paragraph.add_run(text)
    if first_run_properties is not None:
        run_properties = deepcopy(first_run_properties)
        # v11 colors its placeholder runs like instructional text; filled values should read as normal text.
        for color in run_properties.findall(qn("w:color")):
            run_properties.remove(color)
        run._r.insert(0, run_properties)
    paragraph.style = style


def insert_paragraph_after(paragraph: Paragraph, text: str, style: str) -> Paragraph:
    new_element = deepcopy(paragraph._p)
    paragraph._p.addnext(new_element)
    new_paragraph = Paragraph(new_element, paragraph._parent)
    numbering = new_element.find(f"{qn('w:pPr')}/{qn('w:numPr')}")
    if numbering is not None:
        numbering.getparent().remove(numbering)
    set_text(new_paragraph, text, style)
    return new_paragraph


def remove(element: BaseOxmlElement) -> None:
    element.getparent().remove(element)


def siblings_between(first: BaseOxmlElement, last: BaseOxmlElement) -> Iterator[BaseOxmlElement]:
    """Body elements from first up to and including last."""
    element = first
    while element is not None:
        yield element
        if element is last:
            return
        element = element.getnext()
    raise SystemExit("v11 template changed: block end not found after its start")


def keep_section_one_only(document: DocumentObject) -> None:
    body = document.element.body
    children = list(body)
    starts = [
        index
        for index, element in enumerate(children)
        if element.tag == qn("w:p") and Paragraph(element, document).style.name == SECTION_HEADING_STYLE
    ]
    start = next(i for i in starts if SECTION_TITLE in Paragraph(children[i], document).text)
    end = next(i for i in starts if i > start)
    kept = children[start:end]

    for element in children:
        if element not in kept and element is not body.sectPr:
            body.remove(element)

    # Section 1 ends with the landscape SoA section break. Make it the document's final section
    # so no empty page follows the SoA.
    last_break = kept[-1].find(f"{qn('w:pPr')}/{qn('w:sectPr')}")
    if last_break is not None:
        body.remove(body.sectPr)
        body.append(last_break)
        remove(kept[-1])

    remove_unmatched_bookmarks(body)
    remove_broken_fields(body)


def remove_unmatched_bookmarks(body: BaseOxmlElement) -> None:
    starts = {element.get(qn("w:id")): element for element in body.iter(qn("w:bookmarkStart"))}
    ends = {element.get(qn("w:id")): element for element in body.iter(qn("w:bookmarkEnd"))}
    for bookmark_id in starts.keys() ^ ends.keys():
        remove(starts.get(bookmark_id, ends.get(bookmark_id)))


def set_cell_text(document: DocumentObject, cell: BaseOxmlElement, text: str, paragraph_index: int = 0) -> None:
    """Put text in one paragraph of a table cell and drop the cell's later paragraphs."""
    paragraphs = cell.findall(qn("w:p"))
    for extra in paragraphs[paragraph_index + 1 :]:
        cell.remove(extra)
    set_text(Paragraph(paragraphs[paragraph_index], document), text, CONTENT_STYLE)


def table_after(paragraph: Paragraph) -> BaseOxmlElement:
    element = paragraph._p.getnext()
    while element is not None and element.tag != qn("w:tbl"):
        element = element.getnext()
    if element is None:
        raise SystemExit(f"v11 template changed: no table after {paragraph.text!r}")
    return element


def build_objectives(document: DocumentObject) -> None:
    """Replace both 1.1.1 examples with the Objective | Estimand table, one row per objective."""
    heading = find_paragraph(document, "Primary and Secondary Objectives and Estimands")
    prose_example_start = find_paragraph(document, EXAMPLE_START, after=heading)
    table = table_after(prose_example_start)
    table_example_end = find_paragraph(document, EXAMPLE_END, after=Paragraph(table.getprevious(), document))

    for element in list(siblings_between(prose_example_start._p, table.getprevious())):
        remove(element)
    remove(table_example_end._p)

    header_row, row_template, *example_rows = table.findall(qn("w:tr"))
    for row in example_rows:
        table.remove(row)
    for cell, variable in zip(row_template.findall(qn("w:tc")), ("row.objective", "row.estimand")):
        set_cell_text(document, cell, f"{{{{r {variable} }}}}")

    loop_start = deepcopy(row_template)
    loop_end = deepcopy(row_template)
    for row, tag in ((loop_start, "{%tr for row in objectives %}"), (loop_end, "{%tr endfor %}")):
        first_cell, *other_cells = row.findall(qn("w:tc"))
        set_cell_text(document, first_cell, tag)
        for cell in other_cells:
            set_cell_text(document, cell, "")
    row_template.addprevious(loop_start)
    row_template.addnext(loop_end)


def build_design_table(document: DocumentObject) -> None:
    table = table_after(find_paragraph(document, "Key aspects of the trial design"))
    rows = [row.findall(qn("w:tc")) for row in table.findall(qn("w:tr"))]

    for row in rows:
        for cell in row:
            for paragraph_element in cell.findall(qn("w:p")):
                Paragraph(paragraph_element, document).style = CONTENT_STYLE

    for (row_index, cell_index), variable in DESIGN_TABLE_VALUE_CELLS.items():
        set_cell_text(document, rows[row_index][cell_index], f"{{{{r {variable} }}}}")
    row_index, cell_index = MASTER_PROTOCOL_CELL
    set_cell_text(document, rows[row_index][cell_index], "{{r master_protocol }}", paragraph_index=1)


def build_field_lines(document: DocumentObject) -> None:
    for starts_with, (new_text, style) in FIELD_LINES.items():
        paragraph = find_paragraph(document, starts_with)
        set_text(paragraph, new_text if new_text is not None else paragraph.text.strip(), style)
    for label, variable in DURATION_VALUE_LINES.items():
        value_line = find_paragraph(document, "[X", after=find_paragraph(document, label))
        set_text(value_line, variable, CONTENT_STYLE)


def build_content_slots(document: DocumentObject) -> None:
    for starts_with, variable in CONTENT_SLOTS_AFTER.items():
        insert_paragraph_after(find_paragraph(document, starts_with), f"{{{{p {variable} }}}}", CONTENT_STYLE)

    soa_heading = find_paragraph(document, "Schedule of Activities")
    example_start = find_paragraph(document, EXAMPLE_START, after=soa_heading)
    example_end = find_paragraph(document, EXAMPLE_END, after=example_start)
    insert_paragraph_after(example_end, "{{p schedule_of_activities }}", CONTENT_STYLE)
    for element in list(siblings_between(example_start._p, example_end._p)):
        remove(element)


def build_template() -> None:
    document = Document(str(V11_SOURCE_PATH))
    keep_section_one_only(document)
    build_objectives(document)
    build_design_table(document)
    build_field_lines(document)
    build_content_slots(document)
    document.save(str(TEMPLATE_PATH))
    print(f"Wrote {TEMPLATE_PATH}")


if __name__ == "__main__":
    build_template()
