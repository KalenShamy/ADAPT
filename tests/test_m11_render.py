import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import paths  # noqa: F401

import docx
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml.ns import qn

from converter.cli import DEFAULT_TEMPLATE
from converter.parts import tag_collection_parts
from converter.place import Placer, move_intervention_tables, place_node
from converter.model import Paragraph, Span
from converter.render.docx import (
    AI_TABLE_FILL, LOW_CONFIDENCE_NOTE, LOW_CONFIDENCE_TEXT_COLOR, MISSING_TEXT, NO_BASIS_NOTE, UNPLACED_APPENDIX_TITLE, DocxRenderer,
    label_text, participants_sentence,
)
from converter.render.renumber import displayed_numbers
from converter.title_page import render_values
from converter.validate import load_schema, validate_document
from minimal_instance_builder import minimal_instance  # noqa: E402  (schema/ is on the path via converter.validate)

CUSTODY = {"origin": "v10_derived", "v10_sources": [], "actor": {"kind": "script", "name": "test"},
           "confidence": "high", "needs_review": False}


def filled(value):
    return {"value": value, "status": "filled", "custody": copy.deepcopy(CUSTODY)}


def block(text, kind="paragraph", heading=None):
    source = {"section_id": "v10:8.4.2", "found_number": "8.4.2", "found_heading": heading, "match": "renumbered",
              "source_indices": [0], "portion": "all"}
    return {"type": kind, "text": text, "style": "Normal", "source_index": 0,
            "custody": {**copy.deepcopy(CUSTODY), "v10_sources": [source]}}


def collection_section(protocol):
    return protocol["sections"]["9"]["subsections"]["9.2"]["subsections"]["9.2.2"]


class Rendered:
    """A protocol rendered to docx: its paragraph texts, table rows and TOC entries."""

    def __init__(self, protocol):
        renderer = DocxRenderer(DEFAULT_TEMPLATE, {}, Path("."), "")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "out.docx"
            renderer.render(protocol, path)
            document = docx.Document(str(path))
        self.document = document
        self.texts = [paragraph.text for paragraph in document.paragraphs]
        self.note_runs = [run for paragraph in document.paragraphs for run in paragraph.runs
                          if run.text == LOW_CONFIDENCE_NOTE]
        self.tables = [[[cell.text for cell in row.cells] for row in table.rows] for table in document.tables]
        self.toc = renderer.toc_entries


class CollectionPartsTests(unittest.TestCase):
    schema = load_schema()

    def protocol(self, blocks):
        protocol = minimal_instance(self.schema)
        section = collection_section(protocol)
        section["status"] = "populated"
        section["content"] = blocks
        return protocol

    def test_blocks_take_the_part_of_the_subheading_above_or_their_source_heading(self):
        protocol = self.protocol([
            block("Care will be taken not to introduce bias.", heading="Method of Detecting AEs and SAEs"),
            block("Assessment of Intensity"),
            block("Mild: transient."),
            block("Recording and Follow-Up of AE and/or SAE", kind="heading"),
            block("The investigator will record all relevant information."),
        ])
        tag_collection_parts(protocol)
        parts = [item.get("part") for item in collection_section(protocol)["content"]]
        self.assertEqual(parts, ["Identification", "Severity", "Severity", "Recording", "Recording"])

    def test_each_part_is_written_once_under_its_label_and_empty_parts_are_marked(self):
        protocol = self.protocol([
            block("Care will be taken not to introduce bias.", heading="Method of Detecting AEs and SAEs"),
            block("Follow-up of AEs and SAEs", kind="heading"),
            block("The investigator follows each participant."),
        ])
        tag_collection_parts(protocol)
        texts = Rendered(protocol).texts
        self.assertEqual(texts.count("Recording"), 1)
        self.assertEqual(texts.count("Follow-up"), 1)
        self.assertEqual(texts[texts.index("Identification") + 1], "Care will be taken not to introduce bias.")
        self.assertEqual(texts[texts.index("Causality") + 1], MISSING_TEXT)
        self.assertEqual(texts[texts.index("Follow-up") + 2], "The investigator follows each participant.")

    def test_unnumbered_sub_labels_take_no_heading_number(self):
        protocol = self.protocol([])
        rendered = Rendered(protocol)
        self.assertNotIn("Recording", [title for _, _, title in rendered.toc])
        self.assertTrue(all(number for _, number, _ in rendered.toc))
        self.assertEqual(displayed_numbers(protocol["sections"]), {})


class OverallDesignLabelTests(unittest.TestCase):
    def test_number_of_participants_is_the_m11_sentence(self):
        fields = {"participant_number_qualifier": filled("Maximum"), "number_of_participants": filled(120),
                  "participant_number_basis": filled("randomly assigned to trial intervention")}
        self.assertEqual(participants_sentence(fields),
                         "A Maximum of 120 participants will be randomly assigned to trial intervention.")

    def test_unknown_qualifier_keeps_the_m11_choice(self):
        fields = {"number_of_participants": filled(40), "participant_number_basis": filled("enrolled")}
        self.assertEqual(participants_sentence(fields), "A [Target/Maximum] of 40 participants will be enrolled.")

    def test_intervention_codes_and_names_go_on_separate_lines(self):
        fields = {"intervention_codes": [filled("BAY 3670549")], "intervention_nonproprietary_names": [filled("asundexian")]}
        self.assertEqual(label_text("Intervention:", fields), "BAY 3670549\nasundexian")


class AmendmentRenderTests(unittest.TestCase):
    schema = load_schema()

    def amended_protocol(self):
        protocol = minimal_instance(self.schema)
        protocol["title_page"]["amendment_identifier"] = filled("Amendment 3")
        details = protocol["amendment_details"]
        details["summary"] = filled("Adds a futility analysis.")
        details["primary_reasons"] = [filled({"code": "C218496", "decode": "Change In Strategy"})]
        details["substantial_impact_on_safety_or_rights"] = filled({"code": "C49487", "decode": "No"})
        fields = protocol["sections"]["12"]["subsections"]["12.3"]["fields"]
        record = minimal_instance(self.schema)["sections"]["12"]["subsections"]["12.3"]["fields"]
        empty = record["original_protocol_approval_date"]
        fields["original_protocol_approval_date"] = filled("2023-01-10")
        fields["prior_amendments"] = [
            {"amendment_identifier": filled("Amendment 1"), "sponsor_approval_date": filled("2023-06-01"),
             "approximate_enrollment": filled("10%"), "enrollment_scope": copy.deepcopy(empty), "changes": []},
            {"amendment_identifier": filled("Amendment 2"), "sponsor_approval_date": filled("2024-02-01"),
             "approximate_enrollment": filled("40%"), "enrollment_scope": copy.deepcopy(empty), "changes": []},
        ]
        return protocol

    def test_current_amendment_block_comes_before_the_toc(self):
        rendered = Rendered(self.amended_protocol())
        self.assertLess(rendered.texts.index("Current Amendment"), rendered.texts.index("Table of Contents"))
        summary = next(table for table in rendered.tables if table[0][0].startswith("Approximate"))
        self.assertEqual(summary[1], ["Reason(s) for Amendment:", "Primary: Change In Strategy\nSecondary: Not applicable"])
        self.assertEqual(summary[2], ["Amendment Summary:", "Adds a futility analysis."])

    def test_prior_amendments_are_newest_first_then_the_original_protocol(self):
        rendered = Rendered(self.amended_protocol())
        table = next(table for table in rendered.tables if table[0][0] == "Document")
        self.assertEqual([row[0] for row in table[1:]], ["Amendment 2", "Amendment 1", "Original Protocol"])
        self.assertEqual(table[1][2], "Approximately 40% enrolled")
        self.assertEqual(table[3][1:], ["2023-01-10", "0"])

    def test_an_original_protocol_gets_only_the_12_3_statement(self):
        rendered = Rendered(minimal_instance(self.schema))
        self.assertNotIn("Current Amendment", rendered.texts)
        self.assertEqual(rendered.texts.count("This protocol has not been amended."), 2)  # Amendment Details and 12.3
        self.assertFalse(any(table[0][0] == "Document" for table in rendered.tables))


class HeadingNumberTests(unittest.TestCase):
    schema = load_schema()

    def test_objectives_and_the_front_matter_appendix_get_numbered_toc_entries(self):
        protocol = minimal_instance(self.schema)
        protocol["sections"]["3"]["subsections"]["3.1"]["objectives"][0]["statement"] = filled("To assess safety.")
        source = {"section_id": "v10:front.unmapped", "found_number": None, "found_heading": "Document History",
                  "match": "protocol_added", "source_indices": [0], "portion": "all"}
        protocol["unplaced_v10_content"] = [{"v10_source": source, "reason": "no_v11_slot",
                                             "blocks": [block("Original protocol, 2023.")]}]
        toc = {title: number for _, number, title in Rendered(protocol).toc}
        self.assertEqual(toc["Primary Objective 1"], "3.1.1")
        self.assertTrue(toc[UNPLACED_APPENDIX_TITLE].startswith("12."))
        self.assertTrue(toc["Document History"].startswith(toc[UNPLACED_APPENDIX_TITLE] + "."))


class TitlePageValueTests(unittest.TestCase):
    def test_signatories_get_one_line_each_and_scope_lists_its_countries(self):
        empty = {"value": None, "status": "missing", "custody": copy.deepcopy(CUSTODY)}
        page = {
            "sponsor_signatories": [
                {"name": filled("Dr A"), "title": filled("Medical Director"), "date": empty,
                 "signature": {"kind": "text", "value": empty}},
                {"name": filled("Dr B"), "title": empty, "date": empty, "signature": {"kind": "text", "value": empty}},
            ],
            "amendment_scope": filled({"code": "C217026", "decode": "Not Global"}),
            "amendment_scope_identifiers": [
                {"kind": "country", "country_or_region": filled({"system": "ISO 3166-1", "code": "JP", "decode": "Japan"})}],
            "sponsor_approval": filled({"date": "2025-03-01"}),
        }
        values = render_values(page)
        self.assertEqual(values["signatory"], "Dr A, Medical Director\nDr B")
        self.assertEqual(values["amendment_scope"], "Not Global, Japan")
        self.assertEqual(values["sponsor_approval"], "2025-03-01")


def outline_node(number, title, blocks=(), children=()):
    return SimpleNamespace(template_number=number, from_template=True, title=title, level=number.count(".") + 1,
                           source_numbers=[number], source_titles=[title], blocks=list(blocks), children=list(children))


class HeldContentTests(unittest.TestCase):
    schema = load_schema()

    def test_container_lead_in_is_held_once_without_copying_its_subsections(self):
        protocol = minimal_instance(self.schema)
        lead_in = Paragraph([Span("Discontinuation is described below.", False, False, False, 0)])
        child_text = Paragraph([Span("Withdrawal at any time.", False, False, False, 0)])
        chapter = outline_node("7", "Discontinuation", [lead_in],
                               [outline_node("7.2", "Withdrawal from the Trial", [child_text])])
        with tempfile.TemporaryDirectory() as folder:
            place_node(protocol, self.schema, Placer(Path(folder), {}), chapter, None)
        held = protocol["sections"]["7"]["additional_subsections"]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["subsections"], [])
        self.assertEqual(protocol["sections"]["7"]["subsections"]["7.2"]["status"], "populated")


class InterventionTableTests(unittest.TestCase):
    schema = load_schema()

    def test_v010_intervention_and_arm_tables_move_from_6_1_to_6(self):
        protocol = minimal_instance(self.schema)
        section = protocol["sections"]["6"]
        description = section["subsections"]["6.1"]
        lead_in = block("Trial interventions are all pre-specified products.")
        intervention_table = {**block(""), "type": "table", "rows": [["Intervention Label", "Drug A"]]}
        arm_table = {**block(""), "type": "table", "rows": [["Arm Title", "Arm A"]]}
        drug_text = block("Drug A will be given by IV infusion.")
        section["status"] = "populated"
        section["content"] = [lead_in]
        description["status"] = "populated"
        description["content"] = [block("Table 8. Study Intervention(s) Administered"), intervention_table,
                                  block("Table 9. Study Arm(s)"), arm_table, drug_text]
        move_intervention_tables(protocol)
        self.assertEqual([item["rows"][0][0] for item in section["content"] if item["type"] == "table"],
                         ["Intervention Label", "Arm Title"])
        self.assertEqual(section["content"][0], lead_in)
        self.assertEqual(section["content"][1]["text"], "Table 8. Study Intervention(s) Administered")
        self.assertEqual(description["content"], [drug_text])


def ai_custody(confidence):
    return {"origin": "ai_generated", "v10_sources": [], "actor": {"kind": "llm", "name": "fake", "model": "fake"},
            "confidence": confidence, "needs_review": True}


class LowConfidenceNoteTests(unittest.TestCase):
    schema = load_schema()

    def drafted(self, confidence):
        protocol = minimal_instance(self.schema)
        section = protocol["sections"]["4"]["subsections"]["4.2"]["subsections"]["4.2.4"]
        section["status"] = "populated"
        section["custody"] = ai_custody(confidence)
        section["content"] = [{"type": "paragraph", "text": "Twelve weeks covers the endpoint.", "style": "Normal",
                               "custody": ai_custody(confidence)}]
        return protocol

    def test_draft_with_no_source_basis_gets_the_note_in_white_on_red(self):
        run, = Rendered(self.drafted("very_low")).note_runs
        self.assertEqual(run.font.highlight_color, WD_COLOR_INDEX.RED)
        self.assertEqual(run.font.color.rgb, LOW_CONFIDENCE_TEXT_COLOR)

    def test_other_ai_drafts_get_no_note(self):
        for confidence in ("high", "medium", "low"):
            self.assertEqual(Rendered(self.drafted(confidence)).note_runs, [], confidence)

    def test_section_the_model_found_no_basis_for_gets_the_human_authorship_marker(self):
        protocol = minimal_instance(self.schema)
        section = protocol["sections"]["4"]["subsections"]["4.2"]["subsections"]["4.2.4"]
        section["title"] = "Rationale for Trial Duration"
        section["status"] = "missing"
        section["custody"] = ai_custody("none")
        section["content"] = []
        self.assertEqual(validate_document(protocol)[0], [])
        rendered = Rendered(protocol)
        heading = len(rendered.texts) - 1 - rendered.texts[::-1].index(section["title"])  # the body heading, not the TOC
        self.assertEqual(rendered.texts[heading + 1], NO_BASIS_NOTE)
        self.assertNotIn(MISSING_TEXT, rendered.texts[heading + 1:heading + 2])
        run, = [run for paragraph in rendered.document.paragraphs for run in paragraph.runs if run.text == NO_BASIS_NOTE]
        self.assertEqual(run.font.highlight_color, WD_COLOR_INDEX.RED)
        self.assertEqual(run.font.color.rgb, LOW_CONFIDENCE_TEXT_COLOR)

    def test_guessed_overall_design_value_gets_the_note_under_its_label(self):
        protocol = minimal_instance(self.schema)
        fields = protocol["sections"]["1"]["subsections"]["1.1"]["subsections"]["1.1.2"]["fields"]
        fields["number_of_arms"] = {"value": 2, "status": "filled", "custody": ai_custody("very_low")}
        rendered = Rendered(protocol)
        label = rendered.texts.index("Number of Arms:")
        self.assertEqual(rendered.texts[label + 1:label + 3], [LOW_CONFIDENCE_NOTE, "2"])


def table(rows, **extra):
    return {**block(""), "type": "table", "rows": rows, **extra}


class TableCaptionTests(unittest.TestCase):
    schema = load_schema()

    def with_content(self, blocks):
        protocol = minimal_instance(self.schema)
        section = protocol["sections"]["4"]["subsections"]["4.2"]["subsections"]["4.2.4"]
        section["title"] = "Rationale for Intervention Duration"
        section["status"] = "populated"
        section["content"] = blocks
        return protocol

    def test_tables_are_numbered_in_order_with_source_or_llm_titles(self):
        rendered = Rendered(self.with_content([
            block("Doses are listed in Table 7."),
            block("Table 7. Dose Levels"), table([["Dose", "mg"]]),
            table([["Arm", "Drug"]], title="Study Arms"),
            table([["Visit", "Day"]]),
        ]))
        self.assertIn("Table 1: Dose Levels", rendered.texts)
        self.assertIn("Table 2: Study Arms", rendered.texts)
        self.assertIn("Table 3: Rationale for Intervention Duration", rendered.texts)  # no title: the section heading
        self.assertNotIn("Table 7. Dose Levels", rendered.texts)
        self.assertIn("Doses are listed in Table 1.", rendered.texts)

    def test_a_sentence_that_starts_with_table_is_not_a_caption(self):
        sentence = "Table 2 shows the doses. Each dose is given once."
        rendered = Rendered(self.with_content([block(sentence), table([["Dose", "mg"]])]))
        self.assertIn(sentence, rendered.texts)

    def test_estimand_tables_are_captioned_with_their_objective(self):
        protocol = minimal_instance(self.schema)
        objective = protocol["sections"]["3"]["subsections"]["3.1"]["objectives"][0]
        objective["statement"] = filled("To assess PFS.")
        objective["estimands"][0]["endpoint"] = filled("PFS by BICR")
        rendered = Rendered(protocol)
        self.assertIn("Table 1: Estimand Characteristics – Primary Objective 1", rendered.texts)

    def test_generated_table_cells_are_yellow_but_headers_and_labels_are_not(self):
        protocol = minimal_instance(self.schema)
        objective = protocol["sections"]["3"]["subsections"]["3.1"]["objectives"][0]
        objective["statement"] = filled("To assess PFS.")
        objective["estimands"][0]["endpoint"] = filled("PFS by BICR")
        table, = Rendered(protocol).document.tables
        header, row = table.rows[0], table.rows[1]
        self.assertEqual([cell_fill(cell) for cell in header.cells], [None, None])
        self.assertEqual([cell_fill(cell) for cell in row.cells], [None, AI_TABLE_FILL])

    def test_source_tables_are_not_filled(self):
        rendered = Rendered(self.with_content([table([["Dose", "mg"], ["10", "mg"]])]))
        filled_cells = [cell for table in rendered.document.tables for row in table.rows for cell in row.cells
                        if cell_fill(cell)]
        self.assertEqual(filled_cells, [])


def cell_fill(cell):
    shading = cell._tc.tcPr.find(qn("w:shd")) if cell._tc.tcPr is not None else None
    return shading.get(qn("w:fill")) if shading is not None else None


if __name__ == "__main__":
    unittest.main()
