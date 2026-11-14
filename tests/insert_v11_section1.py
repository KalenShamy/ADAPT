"""Insert the Section 1 JSON from extract_v10_section1.py into the v11 Section 1 template.

Text values go in as plain text. Content blocks (paragraphs, tables, figures) are copied from the
v10 file named in the JSON so their formatting survives. Every v11 slot that has no v10 value is
filled with a red "[NOT EXTRACTED: ...]" marker.

Usage: python insert_v11_section1.py <section1.json> [--output <v11 section 1.docx>]
"""

import argparse
import json
from collections import Counter
from copy import deepcopy
from io import BytesIO
from pathlib import Path

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn
from docx.oxml.xmlchemy import BaseOxmlElement
from docxtpl import DocxTemplate, RichText, RichTextParagraph
from docx.oxml import OxmlElement
from lxml import etree
from lxml.etree import _Element

from build_v11_section1_template import TEMPLATE_PATH
from convert_v10_title_page import visible_text
from extract_v10_section1 import UNSURE
from render_v11_title_page import remove_broken_fields, remove_instructional_text

NOT_EXTRACTED_FILL = "FF0000"
NOT_EXTRACTED_TEXT_COLOR = "FFFFFF"
NO_V10_SOURCE = "no v10 field, choose from M11 Controlled Terminology"

RELATIONSHIP_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
# Tracked-change and annotation markup. Deleted content goes; inserted content stays.
REMOVED_TAGS = tuple(
    qn(tag)
    for tag in (
        "w:del",
        "w:moveFrom",
        "w:bookmarkStart",
        "w:bookmarkEnd",
        "w:commentRangeStart",
        "w:commentRangeEnd",
        "w:proofErr",
        "w:rPrChange",
        "w:pPrChange",
        "w:tblPrChange",
        "w:trPrChange",
        "w:tcPrChange",
        "w:sectPrChange",
        "w:tblGridChange",
        "w:numberingChange",
    )
)
UNWRAPPED_TAGS = (qn("w:ins"), qn("w:moveTo"), qn("w:smartTag"))
NOTE_REFERENCE_TAGS = (qn("w:footnoteReference"), qn("w:endnoteReference"), qn("w:commentReference"))
STYLE_REFERENCE_TAGS = (qn("w:pStyle"), qn("w:rStyle"), qn("w:tblStyle"))
STYLE_LINK_TAGS = (qn("w:basedOn"), qn("w:link"), qn("w:next"))
DRAWING_ID_TAGS = (qn("wp:docPr"), qn("pic:cNvPr"))
# v10 authors often type real text over the example; it is protocol text once copied.
V10_EXAMPLE_STYLE = "CPT_Example"
COPIED_TEXT_STYLE = "Normal"
FIRST_COPIED_DRAWING_ID = 10000


class CopiedContent:
    """Body XML for a {{p }} slot. Stands in for docxtpl's Subdoc, which needs the docxcompose package."""

    def __init__(self, elements: list[_Element]) -> None:
        self.xml = "".join(etree.tostring(element, encoding="unicode") for element in elements)

    def __html__(self) -> str:
        return self.xml

    __str__ = __html__


def not_extracted(reason: str) -> RichText:
    return RichText(
        f"[NOT EXTRACTED: {reason}]", bold=True, color=NOT_EXTRACTED_TEXT_COLOR, highlight=NOT_EXTRACTED_FILL
    )


def not_extracted_paragraph(reason: str) -> RichTextParagraph:
    paragraph = RichTextParagraph()
    paragraph.add(not_extracted(reason))
    return paragraph


def text_or_marker(value: object, reason: str) -> RichText:
    if not value or value == UNSURE:
        return not_extracted(reason)
    return RichText(str(value))


class V10ContentCopier:
    """Copies v10 body elements into the template, bringing their images, styles and lists along."""

    def __init__(self, v10_path: Path, template: DocxTemplate) -> None:
        self.v10 = Document(str(v10_path))
        self.v10_elements = list(self.v10.element.body)
        self.template = template
        self.target_part = template.docx.part
        self.style_ids: dict[str, str] = {}
        self.numbering_ids: dict[str, str] = {}
        self.next_drawing_id = FIRST_COPIED_DRAWING_ID
        self.dropped: Counter[str] = Counter()
        self.example_style_id = self.style_id_by_name(self.v10.styles.element, V10_EXAMPLE_STYLE)

    def content(self, elements: list[_Element]) -> CopiedContent:
        container = OxmlElement("w:body")
        for element in elements:
            self.prepare(element)
            container.append(element)
        remove_broken_fields(container)
        return CopiedContent(list(container))

    def block_element(self, block: dict) -> _Element:
        element = deepcopy(self.v10_elements[block["source_index"]])
        if block["type"] == "paragraph" and visible_text(element).strip() != block["text"]:
            raise SystemExit("The v10 file changed since extraction. Run extract_v10_section1.py again.")
        return element

    def inline_value_element(self, field: dict) -> _Element:
        """The label paragraph with the label removed, so only the text after the colon remains."""
        element = deepcopy(self.v10_elements[field["label_source_index"]])
        self.accept_tracked_changes(element)
        strip_leading_characters(element, field["label_length"])
        style = element.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
        if style is not None:
            style.getparent().remove(style)
        return element

    def field_elements(self, field: dict) -> list[_Element]:
        elements = []
        inline_value = str(field["inline_value"])
        # A bare number ("Number of Participants: 70") is placed in its own v11 slot, not copied as text.
        if inline_value and inline_value != UNSURE and not inline_value.isdigit():
            elements.append(self.inline_value_element(field))
        elements.extend(self.block_element(block) for block in field["content"])
        return elements

    def prepare(self, element: _Element) -> None:
        self.accept_tracked_changes(element)
        for section_break in element.iter(qn("w:sectPr")):
            section_break.getparent().remove(section_break)
        for reference in list(element.iter(*NOTE_REFERENCE_TAGS)):
            self.dropped["footnote/comment references"] += reference.tag != qn("w:commentReference")
            run = reference.getparent()
            run.getparent().remove(run)
        self.copy_relationships(element)
        self.copy_styles(element)
        self.copy_numbering(element)
        for drawing_id in element.iter(*DRAWING_ID_TAGS):
            drawing_id.set("id", str(self.next_drawing_id))
            self.next_drawing_id += 1

    def accept_tracked_changes(self, element: _Element) -> None:
        for removed in list(element.iter(*REMOVED_TAGS)):
            if removed.getparent() is not None:
                removed.getparent().remove(removed)
        for wrapper in list(element.iter(*UNWRAPPED_TAGS)):
            parent = wrapper.getparent()
            for child in list(wrapper):
                wrapper.addprevious(child)
            parent.remove(wrapper)

    def copy_relationships(self, element: _Element) -> None:
        for node in list(element.iter()):
            for attribute, old_id in list(node.attrib.items()):
                if not attribute.startswith(RELATIONSHIP_NS):
                    continue
                relationship = self.v10.part.rels.get(old_id)
                if relationship is None:
                    del node.attrib[attribute]
                elif relationship.is_external:
                    node.set(attribute, self.target_part.relate_to(relationship.target_ref, relationship.reltype, True))
                elif relationship.reltype == RT.IMAGE:
                    new_id, _ = self.target_part.get_or_add_image(BytesIO(relationship.target_part.blob))
                    node.set(attribute, new_id)
                else:
                    self.dropped[f"embedded objects ({relationship.reltype.rsplit('/', 1)[-1]})"] += 1
                    run = next(node.iterancestors(qn("w:r")), node)
                    if run.getparent() is not None:
                        run.getparent().remove(run)

    def copy_styles(self, element: _Element) -> None:
        for reference in element.iter(*STYLE_REFERENCE_TAGS):
            style_id = reference.get(qn("w:val"))
            if style_id == self.example_style_id:
                reference.set(qn("w:val"), self.target_style_id(COPIED_TEXT_STYLE))
            else:
                reference.set(qn("w:val"), self.target_style_id(style_id))

    def target_style_id(self, v10_style_id: str) -> str:
        """The v11 style with the same name, or a copy of the v10 style if v11 has none."""
        if v10_style_id in self.style_ids:
            return self.style_ids[v10_style_id]
        target_styles = self.template.docx.styles.element
        source_style = self.v10.styles.element.get_by_id(v10_style_id)
        if source_style is None:
            self.style_ids[v10_style_id] = v10_style_id
            return v10_style_id
        name = source_style.name_val
        same_name = self.style_id_by_name(target_styles, name) if name else None
        if same_name:
            self.style_ids[v10_style_id] = same_name
            return same_name

        self.style_ids[v10_style_id] = v10_style_id
        copied_style = deepcopy(source_style)
        target_styles.append(copied_style)
        for link in copied_style.iter(*STYLE_LINK_TAGS):
            link.set(qn("w:val"), self.target_style_id(link.get(qn("w:val"))))
        return v10_style_id

    @staticmethod
    def style_id_by_name(styles: BaseOxmlElement, name: str) -> str | None:
        style = styles.get_by_name(name)
        return style.styleId if style is not None else None

    def copy_numbering(self, element: _Element) -> None:
        for numbering_id in element.iter(qn("w:numId")):
            old_id = numbering_id.get(qn("w:val"))
            if old_id != "0":
                numbering_id.set(qn("w:val"), self.target_numbering_id(old_id))

    def target_numbering_id(self, v10_numbering_id: str) -> str:
        """Copy a v10 list definition into the v11 numbering part under new ids."""
        if v10_numbering_id in self.numbering_ids:
            return self.numbering_ids[v10_numbering_id]
        source = self.v10.part.numbering_part.element
        target = self.target_part.numbering_part.element
        source_num = next(n for n in source.findall(qn("w:num")) if n.get(qn("w:numId")) == v10_numbering_id)
        abstract_id = source_num.find(qn("w:abstractNumId")).get(qn("w:val"))
        source_abstract = next(
            a for a in source.findall(qn("w:abstractNum")) if a.get(qn("w:abstractNumId")) == abstract_id
        )

        target_abstracts = target.findall(qn("w:abstractNum"))
        new_abstract_id = str(max((int(a.get(qn("w:abstractNumId"))) for a in target_abstracts), default=-1) + 1)
        new_abstract = deepcopy(source_abstract)
        new_abstract.set(qn("w:abstractNumId"), new_abstract_id)
        # nsid ties lists together across documents; without it Word won't merge this list into another.
        for list_id in new_abstract.findall(qn("w:nsid")):
            new_abstract.remove(list_id)
        target_abstracts[-1].addnext(new_abstract)

        target_nums = target.findall(qn("w:num"))
        new_numbering_id = str(max(int(n.get(qn("w:numId"))) for n in target_nums) + 1)
        new_num = deepcopy(source_num)
        new_num.set(qn("w:numId"), new_numbering_id)
        new_num.find(qn("w:abstractNumId")).set(qn("w:val"), new_abstract_id)
        target_nums[-1].addnext(new_num)

        self.numbering_ids[v10_numbering_id] = new_numbering_id
        return new_numbering_id


def strip_leading_characters(paragraph: _Element, count: int) -> None:
    """Drop the first `count` characters of visible text, counting tabs and breaks as one, as visible_text does."""
    for node in list(paragraph.iter(qn("w:t"), qn("w:tab"), qn("w:br"))):
        if count <= 0:
            return
        if node.tag == qn("w:t"):
            text = node.text or ""
            node.text = text[count:]
            count -= len(text)
        else:
            node.getparent().remove(node)
            count -= 1


def objective_rows(objectives: dict | str) -> list[dict[str, RichText]]:
    if objectives == UNSURE or objectives["rows"] == UNSURE:
        return [
            {
                "objective": not_extracted("v10 synopsis objectives table not found or not readable"),
                "estimand": not_extracted("estimand"),
            }
        ]
    rows = []
    for row in objectives["rows"]:
        if row["category"] == "exploratory":
            continue
        objective = RichText()
        if row["category_label"] == UNSURE:
            objective.add(not_extracted("objective category (primary/secondary)"))
            objective.add(" ")
        else:
            objective.add(f"{row['category_label']}: ", bold=True)
        objective.add(text_or_marker(row["objective"], "objective text"))

        estimand = RichText()
        if row["estimand"] != UNSURE:
            estimand.add(row["estimand"])
        else:
            if row["endpoint"] != UNSURE:
                estimand.add(f"Endpoint: {row['endpoint']}\n")
            estimand.add(not_extracted("estimand, v10 gives the endpoint only"))
        rows.append({"objective": objective, "estimand": estimand})
    return rows


def copied_content(copier: V10ContentCopier, field: dict | str, reason: str) -> CopiedContent | RichTextParagraph:
    if field == UNSURE:
        return not_extracted_paragraph(reason)
    elements = copier.field_elements(field) if "label" in field else [copier.block_element(b) for b in field["content"]]
    if not elements:
        return not_extracted_paragraph(reason)
    return copier.content(elements)


def build_context(extracted: dict, copier: V10ContentCopier) -> dict[str, object]:
    subsections = extracted["section_1"]["subsections"]
    synopsis = subsections["synopsis"]
    fields = synopsis["fields"] if synopsis != UNSURE else {}
    participants = fields.get("number_of_participants", UNSURE)
    brief_summary = fields.get("brief_summary", UNSURE)
    durations = brief_summary["values"] if brief_summary != UNSURE else {}
    committees = fields.get("committees", UNSURE)
    committee_hint = f"see v10 '{committees['label']}'" if committees != UNSURE else "no v10 committee field found"

    participant_count = participants["inline_value"] if participants != UNSURE else UNSURE
    design_fields = {
        "intervention": "Investigational Product Code(s) or nonproprietary name(s)",
        "population_type": f"Population Type, {NO_V10_SOURCE}",
        "intervention_model": f"Intervention Model, {NO_V10_SOURCE}",
        "population_diagnosis_or_condition": "Population Diagnosis or Condition (MedDRA Preferred Term)",
        "control_type": f"Control Type, {NO_V10_SOURCE}",
        "population_age": "Population Age, see v10 inclusion criteria",
        "control_description": "Control Description",
        "site_distribution_and_geographic_scope": f"Site Distribution and Geographic Scope, {NO_V10_SOURCE}",
        "intervention_assignment_method": f"Intervention Assignment Method, {NO_V10_SOURCE}",
        "master_protocol": "Master Protocol yes/no",
        "stratification": "Stratification yes/no",
        "drug_device_combination_product": "Drug/Device Combination Product yes/no",
        "adaptive_trial_design": "Adaptive Trial Design yes/no",
        "number_of_arms": "Number of Arms",
        "trial_blind_schema": f"Trial Blind Schema, {NO_V10_SOURCE}",
        "blinded_roles": f"Blinded Roles, {NO_V10_SOURCE}",
        "participant_target_or_maximum": "Target or Maximum",
        "participant_assignment": "randomly assigned to trial intervention / enrolled",
        "independent_committees": f"Independent Committees, {committee_hint}",
        "other_committees": f"Other committees, {committee_hint}",
    }

    return {key: not_extracted(reason) for key, reason in design_fields.items()} | {
        "objectives": objective_rows(fields.get("objectives", UNSURE)),
        "participant_count": text_or_marker(
            participant_count if str(participant_count).isdigit() else UNSURE, "number of participants"
        ),
        "number_of_participants_details": copied_content(
            copier, participants, "v10 Number of Participants not found"
        ),
        "intervention_duration": text_or_marker(
            durations.get("treatment_duration"), "treatment duration not found in v10 Brief Summary"
        ),
        "participation_duration": text_or_marker(
            durations.get("study_duration"), "study duration not found in v10 Brief Summary"
        ),
        "duration_details": copied_content(
            copier, fields.get("study_arms_and_duration", UNSURE), "v10 Study Arms and Duration not found"
        ),
        "schema": copied_content(copier, subsections["schema"], "v10 1.2 Schema not found"),
        "schedule_of_activities": copied_content(
            copier, subsections["schedule_of_activities"], "v10 1.3 Schedule of Activities not found"
        ),
    }


def report(extracted: dict, copier: V10ContentCopier) -> None:
    synopsis = extracted["section_1"]["subsections"]["synopsis"]
    if synopsis != UNSURE:
        no_v11_slot = ("protocol_title", "brief_title", "regulatory_identifiers", "pediatric_plan_number",
                       "rationale", "overall_design", "committees")
        dropped = [synopsis["fields"][key]["label"] for key in no_v11_slot if synopsis["fields"][key] != UNSURE]
        print(f"v10 synopsis fields with no v11 Section 1 slot (title page, 2.1.1, 4.1, 11.4): {', '.join(dropped)}")
        unrecognized = [field["label"] for field in synopsis["unrecognized_fields"]]
        if unrecognized:
            print(f"Unrecognized v10 synopsis fields, not inserted: {', '.join(unrecognized)}")
        objectives = synopsis["fields"]["objectives"]
        if objectives != UNSURE and objectives["rows"] != UNSURE:
            exploratory = sum(row["category"] == "exploratory" for row in objectives["rows"])
            if exploratory:
                print(f"Left out {exploratory} exploratory objective(s); v11 1.1.1 is primary and secondary only")
    for item, count in copier.dropped.items():
        print(f"Dropped {count} {item} from copied v10 content")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("section1_json", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    extracted = json.loads(args.section1_json.read_text(encoding="utf-8"))
    v10_path = Path(extracted["source_path"])
    output_path = args.output or args.section1_json.with_name(
        f"{v10_path.stem.replace('v10', 'v11')}_section1.docx"
    )

    template = DocxTemplate(TEMPLATE_PATH)
    template.init_docx()
    remove_instructional_text(template)
    copier = V10ContentCopier(v10_path, template)
    context = build_context(extracted, copier)
    missing = template.get_undeclared_template_variables() - context.keys()
    if missing:
        raise SystemExit(f"Context is missing: {sorted(missing)}")
    template.render(context, autoescape=True)
    template.save(output_path)
    print(f"Wrote {output_path}")
    report(extracted, copier)


if __name__ == "__main__":
    main()
