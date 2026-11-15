import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from converter.generate.fields import AMENDMENT_DETAILS, amendment_details_extraction, extract, section_at, section_code
from converter.validate import load_schema, validate_document
from minimal_instance_builder import minimal_instance  # noqa: E402  (schema/ is on the path via converter.validate)

CODES = {"1.1.2 Overall Design": "C1", "5.2 Exclusion Criteria": "C2", "10.4 Analyses Associated with the Primary Objective(s)": "C3"}


class FakeClient:
    name = "fake"
    model = "fake"

    def __init__(self, answer):
        self.answer = answer

    def complete(self, prompt):
        return json.dumps(self.answer)


class SectionCodeTests(unittest.TestCase):
    def test_matches_on_title_even_when_the_number_differs(self):
        self.assertEqual(section_code(CODES, "Section 5.3 Exclusion Criteria"), {"code": "C2", "decode": "5.2 Exclusion Criteria"})

    def test_title_only_matches(self):
        self.assertEqual(section_code(CODES, "overall design")["code"], "C1")

    def test_unknown_title_or_number_only_does_not_match(self):
        self.assertIsNone(section_code(CODES, "Section 5.2"))
        self.assertIsNone(section_code(CODES, "Study Drug Handling"))


class AmendmentDetailsTests(unittest.TestCase):
    schema = load_schema()

    def protocol(self):
        protocol = minimal_instance(self.schema)
        source = {"section_id": "v10:front.unmapped", "found_number": None, "found_heading": "Summary of Changes",
                  "match": "protocol_added", "source_indices": [0], "portion": "all"}
        custody = {"origin": "v10_derived", "v10_sources": [source], "actor": {"kind": "script", "name": "t"},
                   "confidence": "high", "needs_review": False}
        block = {"type": "paragraph", "text": "Amendment 2 adds a lower age limit to the exclusion criteria.",
                 "style": "Normal", "source_index": 0, "custody": custody}
        protocol["unplaced_v10_content"] = [{"v10_source": source, "reason": "no_v11_slot", "blocks": [block]}]
        return protocol

    def test_changes_get_a_section_code_and_a_summary_fills_the_block(self):
        client = FakeClient({
            "summary": "Adds a lower age limit.",
            "approximate_enrolled.quantity": 40,
            "approximate_enrolled.unit": "percent",
            "changes": [
                {"description": "Age limit added", "rationale": "Safety", "section": "Section 5.3 Exclusion Criteria"},
                {"description": "Typo fixed", "rationale": "Clarity", "section": "Appendix Z"},
            ],
        })
        extraction = amendment_details_extraction(self.schema)
        result = extract(self.protocol(), client, self.schema, lambda p: validate_document(p)[0], extraction)
        details = section_at(result, AMENDMENT_DETAILS)
        self.assertEqual(details["summary"]["value"], "Adds a lower age limit.")
        self.assertEqual(details["approximate_enrolled"]["quantity"]["value"], 40)
        first, second = details["changes"]
        self.assertEqual(first["section"]["status"], "filled")
        self.assertTrue(first["section"]["value"]["decode"].endswith("Exclusion Criteria"))
        self.assertEqual((second["section"]["status"], second["section"]["raw_text"]), ("unsure", "Appendix Z"))
        self.assertEqual(validate_document(result)[0], [])


if __name__ == "__main__":
    unittest.main()
