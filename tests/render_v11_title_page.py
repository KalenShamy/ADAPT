from collections.abc import Mapping
from pathlib import Path

from docx.oxml.ns import qn
from docx.oxml.xmlchemy import BaseOxmlElement
from docxtpl import DocxTemplate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = PROJECT_ROOT / "resources" / "CPT_CoreBWE-v011_TitlePage_docxtpl.docx"
SAMPLE_OUTPUT_PATH = Path(__file__).resolve().parent / "title_page_filled.docx"

INSTRUCTIONAL_STYLES = {"CPT_Instructional", "CPT_Example"}
FIELD_LABEL_STYLE = "Heading (No TOC)"
TEMPLATE_VARIABLE_MARKER = "{{"

# Field labels that are also template variables
FIELD_LABELS = {
    "Sponsor_Confidentiality_Statement": "Confidential",
    "Trial_Acronym": "Trial Acronym",
    "Version_Number": "Version Number",
    "Version_Date": "Version Date",
    "Amendment_Identifier": "Amendment Identifier",
    "Amendment_Scope": "Amendment Scope",
    "Investigational_Product_Code_s": "Investigational Product Code(s)",
    "Investigational_Product_Name_s": "Investigational Product Name(s)",
    "Short_Title": "Short Title",
    "Device_Manufacturer_Name_and_Legal_Address": "Device Manufacturer Name and Legal Address",
    "Medical_Expert_Contact": "Medical Expert Contact:",
}

SAMPLE_VALUES = {
    # Full title
    "intervention_model": "parallel-group treatment",
    "primary_purpose": "treatment",
    "trial_phase": "Phase 2",
    "blinding": "double-blind",
    "number": "2",
    "health_measurement_outcome": "the safety and effectiveness",
    "investigational_intervention": "Addiryn",
    "intervention_form": "tablets",
    "condition_disease": "agitation in dementia",
    "male_and_or_female": "male and female",
    "X_to_X_years_of_age": "18 to 65 years of age",
    # Identifiers
    "Trial_phase": "Phase 2",
    "protocol_number": "ABC-123-001",
    "yes_no": "Yes",
    "amendment_identifier": "Amendment 1",
    "Amendment_Scope_Type": "Not Global",
    "Country_Identifier": "United States",
    "Region_Identifier": "North America",
    "Enter_Site_Identifier": "Site 101",
    "name": "Addiryn",
    "insert_device_manufacturer_and_address": "Acme Devices Inc., 1 Main St, Springfield, IL 62701, USA",
    # Sponsor
    "Sponsor_Co_sponsor_Name": "Example Pharma Inc.",
    "Sponsor_Co_sponsor_Legal_Address": "100 Research Way, Cambridge, MA 02139, USA",
    "Local_Sponsor_Name": "Example Pharma GmbH",
    "Local_Sponsor_Legal_Address": "Musterstrasse 5, 80331 Munich, Germany",
    # Regulatory identifier table
    "Agency": "FDA",
    "ID_Number": "IND 123456",
    # Signatory table
    "Name": "Jane Doe, MD",
    "Title": "Chief Medical Officer",
    "Date": "01 Jan 2026",
    "will_be_provided_separately_OR_can_be_found_in_XX": "will be provided separately",
}


def remove_instructional_text(template: DocxTemplate) -> None:
    for paragraph in list(template.docx.paragraphs):
        if paragraph.style.name not in INSTRUCTIONAL_STYLES:
            continue
        if TEMPLATE_VARIABLE_MARKER in paragraph.text:
            # A field label the template author left in an instructional style; show it like the other labels.
            paragraph.style = FIELD_LABEL_STYLE
        else:
            paragraph._element.getparent().remove(paragraph._element)
    remove_broken_fields(template.docx.element.body)


def remove_broken_fields(body: BaseOxmlElement) -> None:
    """Drop the code of any field that lost its begin or end marker.

    A Word field (hyperlink, page ref, ...) is a begin marker, field code, a separate marker,
    display text and an end marker, and it can span paragraphs. Removing paragraphs can leave
    half a field behind, and Word then refuses to open the file. The display text is kept.
    """
    open_fields: list[list[BaseOxmlElement]] = []
    orphan_runs: list[BaseOxmlElement] = []
    for element in body.iter(qn("w:fldChar"), qn("w:instrText")):
        run = element.getparent()
        if element.tag == qn("w:instrText"):
            (open_fields[-1] if open_fields else orphan_runs).append(run)
            continue
        marker_type = element.get(qn("w:fldCharType"))
        if marker_type == "begin":
            open_fields.append([run])
        elif not open_fields:
            orphan_runs.append(run)
        elif marker_type == "separate":
            open_fields[-1].append(run)
        else:
            open_fields.pop()

    unclosed_field_runs = [run for field_runs in open_fields for run in field_runs]
    for run in orphan_runs + unclosed_field_runs:
        if run.getparent() is not None:
            run.getparent().remove(run)


def render_title_page(context: Mapping[str, str], output_path: Path, remove_instructions: bool = False) -> None:
    template = DocxTemplate(TEMPLATE_PATH)
    if remove_instructions:
        template.init_docx()
        remove_instructional_text(template)
    missing = template.get_undeclared_template_variables() - context.keys()
    if missing:
        raise SystemExit(f"Context is missing: {sorted(missing)}")
    # Protocol text can contain &, < or >, which would break the document XML unescaped.
    template.render(context, autoescape=True)
    template.save(output_path)
    print(f"Wrote {output_path}")


def main() -> None:
    render_title_page(FIELD_LABELS | SAMPLE_VALUES, SAMPLE_OUTPUT_PATH)


if __name__ == "__main__":
    main()
