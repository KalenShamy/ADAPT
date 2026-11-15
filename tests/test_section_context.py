import json
import unittest
from unittest import mock

import paths  # noqa: F401

from converter.generate import context as context_module
from converter.generate.context import (
    CROSS_REFERENCED, EXPECTED, OWN, SURROUNDING, SYNOPSIS, SourceIndex, section_context,
)
from converter.generate.fields import Context, build_prompt, interventions_extraction
from converter.generate.fill import generate_missing
from converter.validate import load_schema

SYNOPSIS_TEXT = "Synopsis: Drug A versus Drug B in adults with lung cancer."
OVERVIEW_TEXT = "Study interventions are all pre-specified investigational products."
INTERVENTION_ROW = ["Intervention Label", "Drug A", "Drug B"]
DRUG_TEXT = "Drug A will be given at 20 mg/kg by intravenous infusion."
CHAPTER_7_TEXT = "Discontinuation of sites is covered elsewhere."
WITHDRAWAL_TEXT = "A participant may withdraw at any time."
UNRELATED_TEXT = "Vital signs include blood pressure and pulse."
PROPHYLAXIS_TEXT = "Participants with detectable HBV DNA will start antiviral prophylaxis."
POSTSTUDY_PREGNANCY_TEXT = "Any poststudy pregnancy-related SAE will be reported to the sponsor."


def source(section_id):
    number = section_id.removeprefix("v10:")
    return {"section_id": section_id, "found_number": number, "found_heading": number, "match": "exact",
            "source_indices": [0], "portion": "all"}


def custody(section_id):
    return {"origin": "v10_derived", "v10_sources": [source(section_id)], "actor": {"kind": "script", "name": "t"},
            "confidence": "high", "needs_review": False}


def paragraph(text, section_id):
    return {"type": "paragraph", "text": text, "style": "Normal", "custody": custody(section_id)}


def section(number, title, blocks=(), subsections=None, status="populated"):
    return {"number": number, "title": title, "status": status, "content": list(blocks),
            "custody": custody(f"v10:{number}"), "additional_subsections": [], "subsections": subsections or {}}


def make_protocol():
    table = {"type": "table", "rows": [INTERVENTION_ROW], "custody": custody("v10:6.1")}
    return {"sections": {
        "1": section("1", "Protocol Summary", status="container", subsections={
            "1.1": section("1.1", "Protocol Synopsis", [paragraph(SYNOPSIS_TEXT, "v10:1.1")])}),
        # the v10 6.1 intervention table was moved up to 6, as M11 wants
        "6": section("6", "Trial Intervention and Concomitant Therapy",
                     [paragraph(OVERVIEW_TEXT, "v10:6"), table], subsections={
                         "6.1": section("6.1", "Description of Investigational Trial Intervention",
                                        [paragraph(DRUG_TEXT, "v10:6.1"), paragraph(PROPHYLAXIS_TEXT, "v10:6.1")]),
                         "6.9": section("6.9", "Description of Noninvestigational Trial Intervention",
                                        status="missing", subsections={
                                            "6.9.1": section("6.9.1", "Background Trial Intervention",
                                                             status="missing")})}),
        # text with no v10 source that any section expects, so it can only arrive as a lead-in
        "7": section("7", "Participant Discontinuation", [paragraph(CHAPTER_7_TEXT, "v10:7.unmapped")], subsections={
            "7.2": section("7.2", "Participant Discontinuation or Withdrawal from the Trial",
                           [paragraph(WITHDRAWAL_TEXT, "v10:7.2")])}),
        "8": section("8", "Trial Assessments and Procedures", status="container", subsections={
            "8.3": section("8.3", "Efficacy Assessments", [paragraph(UNRELATED_TEXT, "v10:8.3")])}),
        "9": section("9", "Adverse Events", status="container", subsections={
            "9.2": section("9.2", "Timing and Procedures for Collection and Reporting", status="missing"),
            "9.3": section("9.3", "Pregnancy and Postpartum Information", status="container", subsections={
                "9.3.1": section("9.3.1", "Participants Who Become Pregnant During the Trial",
                                 [paragraph(POSTSTUDY_PREGNANCY_TEXT, "v10:9.3.1.unmapped")])})}),
    }}


def tiers(chosen, text):
    return [pick.tier for pick in chosen.picks if text in pick.unit.text]


class SectionContextTests(unittest.TestCase):
    def setUp(self):
        self.index = SourceIndex.build(make_protocol())

    def test_own_content_and_synopsis_are_always_included(self):
        chosen = section_context(self.index, "6.1")
        self.assertEqual(tiers(chosen, DRUG_TEXT), [OWN])
        self.assertEqual(tiers(chosen, SYNOPSIS_TEXT), [SYNOPSIS])

    def test_expected_v10_content_is_found_where_it_was_placed(self):
        chosen = section_context(self.index, "6.1")
        self.assertEqual(tiers(chosen, "Intervention Label"), [EXPECTED])

    def test_unrelated_sections_are_left_out(self):
        self.assertNotIn(UNRELATED_TEXT, section_context(self.index, "6.1").text)

    def test_cross_reference_brings_only_the_referenced_sections_own_text(self):
        chosen = section_context(self.index, "6")  # chapter 7's template refers to Section 6
        self.assertEqual(tiers(chosen, CHAPTER_7_TEXT), [CROSS_REFERENCED])
        self.assertNotIn(WITHDRAWAL_TEXT, chosen.text)

    def test_subsection_inherits_what_its_parent_expects(self):
        chosen = section_context(self.index, "6.9.1")  # the crosswalk sends v10 6.1 to 6.9 as well as 6.1
        self.assertEqual(tiers(chosen, PROPHYLAXIS_TEXT), [EXPECTED])
        self.assertIn("expected for 6.9", next(pick.reason for pick in chosen.picks
                                               if PROPHYLAXIS_TEXT in pick.unit.text))

    def test_cross_reference_to_a_short_heading_only_section_brings_its_subsections(self):
        chosen = section_context(self.index, "9.2")  # 9.2's template refers to 9.3, which has no text of its own
        self.assertEqual(tiers(chosen, POSTSTUDY_PREGNANCY_TEXT), [CROSS_REFERENCED])

    def test_parent_lead_in_surrounds_a_subsection(self):
        chosen = section_context(self.index, "7.2")
        self.assertEqual(tiers(chosen, CHAPTER_7_TEXT), [SURROUNDING])

    def test_blocks_are_written_in_document_order_under_their_headings(self):
        text = section_context(self.index, "6.1").text
        self.assertLess(text.index(SYNOPSIS_TEXT), text.index("Intervention Label"))
        self.assertLess(text.index("Intervention Label"), text.index(DRUG_TEXT))
        self.assertIn("## 6.1 Description of Investigational Trial Intervention", text)

    def test_related_content_stays_within_its_budget(self):
        with mock.patch.object(context_module, "RELATED_MAX_CHARS", 0):
            chosen = section_context(self.index, "6.1")
        self.assertEqual({pick.tier for pick in chosen.picks}, {OWN, SYNOPSIS})

    def test_own_content_over_the_ceiling_is_cut_and_reported(self):
        with mock.patch.object(context_module, "CONTEXT_CEILING_CHARS", context_module.SYNOPSIS_MAX_CHARS):
            chosen = section_context(self.index, "6.1")
        self.assertEqual(chosen.own_blocks_left_out, 2)
        self.assertNotIn(DRUG_TEXT, chosen.text)

    def test_context_carries_the_sources_of_what_it_holds(self):
        section_ids = {source["section_id"] for source in section_context(self.index, "6.1").sources}
        self.assertIn("v10:6.1", section_ids)
        self.assertNotIn("v10:8.3", section_ids)


class FakeClient:
    name = "fake"
    model = "fake-model"

    def __init__(self):
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return json.dumps({"status": "populated", "paragraphs": ["Drafted."], "confidence": "low"})


class PromptTests(unittest.TestCase):
    def test_drafting_prompt_has_the_m11_format_and_only_related_text(self):
        protocol = make_protocol()
        protocol["sections"]["6"]["subsections"]["6.5"] = section(
            "6.5", "Management of Investigational Trial Intervention Overdose", status="missing")
        client = FakeClient()
        generate_missing(protocol, client)
        prompt, = [prompt for prompt in client.prompts if "SECTION TO DRAFT: 6.5 " in prompt]
        self.assertIn("ICH M11 FORMAT FOR THIS SECTION", prompt)
        self.assertIn("Content: <Enter Management of Investigational Trial Intervention Overdose>", prompt)
        self.assertIn(OVERVIEW_TEXT, prompt)
        self.assertNotIn(UNRELATED_TEXT, prompt)

    def test_extraction_prompt_has_the_m11_format(self):
        prompt = build_prompt(interventions_extraction(load_schema()), Context("source text", []))
        self.assertIn("ICH M11 FORMAT FOR 6", prompt)
        self.assertIn("<IMP or NIMP>", prompt)


if __name__ == "__main__":
    unittest.main()
