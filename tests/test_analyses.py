import json
import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from converter.generate.fields import extract_objectives, extract_analyses, fill_summary_rows, section_at
from converter.validate import load_schema, validate_document
from minimal_instance_builder import minimal_instance  # noqa: E402  (schema/ is on the path via converter.validate)

ANSWER = {
    "primary": [{"category": "Efficacy", "statement": "Compare drug with placebo",
                 "estimands": [{"endpoint": "Time to first event", "population": "Adults"}]}],
    "secondary": [],
    "exploratory": [],
    "primary-1": {"statistical_analysis_method": ["Cox model stratified by region."],
                  "handling_of_missing_data": ["Censor at last contact."],
                  "sensitivity_analysis": []},
}


class FakeClient:
    name = "fake"
    model = "fake"

    def complete(self, prompt):
        return json.dumps(ANSWER)


def protocol_with_source(schema):
    protocol = minimal_instance(schema)
    section = section_at(protocol, "10.4")
    source = {"section_id": "v10:9.3", "found_number": "9.3", "found_heading": "Analyses", "match": "exact",
              "source_indices": [0], "portion": "all"}
    custody = {"origin": "v10_derived", "v10_sources": [source], "actor": {"kind": "script", "name": "t"},
               "confidence": "high", "needs_review": False}
    section["additional_subsections"] = [{
        "number": None, "title": "Primary Analyses", "level": 4, "status": "populated", "heading_source": {**source, "section_id": "v10:9.unmapped", "match": "protocol_added"},
        "custody": custody, "subsections": [],
        "content": [{"type": "paragraph", "text": "Cox model stratified by region.", "style": "Normal",
                     "source_index": 0, "custody": custody}]}]
    return protocol


class AnalysesTests(unittest.TestCase):
    schema = load_schema()

    def validate(self, protocol):
        return validate_document(protocol)[0]

    def run_steps(self):
        client = FakeClient()
        result = extract_objectives(protocol_with_source(self.schema), client, self.schema, self.validate)
        result = fill_summary_rows(result, client, self.validate)
        return extract_analyses(result, client, self.schema, self.validate)

    def test_each_analysis_heading_keeps_its_own_confidence(self):
        answer = {**ANSWER, "primary-1": {
            "statistical_analysis_method": {"paragraphs": ["Cox model stratified by region."], "confidence": "high"},
            "sensitivity_analysis": {"paragraphs": ["A tipping-point analysis."], "confidence": "very_low"}}}
        client = FakeClient()
        with mock.patch.object(FakeClient, "complete", lambda _, prompt: json.dumps(answer)):
            result = extract_objectives(protocol_with_source(self.schema), client, self.schema, self.validate)
            result = extract_analyses(result, client, self.schema, self.validate)
        parts = section_at(result, "10.4")["objective_analyses"][0]["subsections"]
        self.assertEqual(parts["statistical_analysis_method"]["custody"]["confidence"], "high")
        self.assertEqual(parts["sensitivity_analysis"]["custody"]["confidence"], "very_low")
        self.assertEqual(parts["sensitivity_analysis"]["content"][0]["custody"]["confidence"], "very_low")

    def test_a_heading_with_no_basis_is_left_for_a_person_or_omitted_when_optional(self):
        no_basis = {"status": "no_basis", "paragraphs": []}
        answer = {**ANSWER, "primary-1": {
            "statistical_analysis_method": {"paragraphs": ["Cox model stratified by region."], "confidence": "high"},
            "handling_of_missing_data": no_basis, "sensitivity_analysis": no_basis}}
        client = FakeClient()
        with mock.patch.object(FakeClient, "complete", lambda _, prompt: json.dumps(answer)):
            result = extract_objectives(protocol_with_source(self.schema), client, self.schema, self.validate)
            result = extract_analyses(result, client, self.schema, self.validate)
        parts = section_at(result, "10.4")["objective_analyses"][0]["subsections"]
        required = parts["handling_of_missing_data"]
        self.assertEqual((required["status"], required["content"]), ("missing", []))
        self.assertEqual(required["custody"]["confidence"], "none")
        self.assertEqual(required["custody"]["origin"], "ai_transformed")
        self.assertEqual(parts["sensitivity_analysis"]["status"], "omitted")
        self.assertEqual(self.validate(result), [])

    def test_synopsis_rows_link_to_the_section_3_objectives(self):
        result = self.run_steps()
        rows = section_at(result, "1.1.1")["fields"]["summary_rows"]
        self.assertEqual([row["objective_id"] for row in rows], ["primary-1"])
        self.assertEqual(rows[0]["objective"]["value"], "Compare drug with placebo")
        self.assertIn("Endpoint: Time to first event", rows[0]["estimand_summary"]["value"])

    def test_each_objective_gets_an_analysis_that_replaces_the_extra_headings(self):
        result = self.run_steps()
        section = section_at(result, "10.4")
        self.assertEqual(section["additional_subsections"], [])
        analysis, = section["objective_analyses"]
        self.assertEqual((analysis["objective_id"], analysis["number"], analysis["status"]),
                         ("primary-1", "10.4.1", "populated"))
        parts = analysis["subsections"]
        self.assertEqual(parts["statistical_analysis_method"]["content"][0]["text"], "Cox model stratified by region.")
        self.assertEqual(parts["handling_of_data"]["status"], "missing")
        self.assertEqual(parts["sensitivity_analysis"]["status"], "omitted")
        self.assertEqual(parts["statistical_analysis_method"]["number"], "10.4.1.1")
        self.assertEqual(self.validate(result), [])

    def test_no_analysis_text_in_the_answer_keeps_the_extra_headings(self):
        client = FakeClient()
        protocol = protocol_with_source(self.schema)
        objectives = extract_objectives(protocol, client, self.schema, self.validate)
        ANSWER_BACKUP = dict(ANSWER)
        try:
            ANSWER.pop("primary-1")
            result = extract_analyses(objectives, client, self.schema, self.validate)
        finally:
            ANSWER.update(ANSWER_BACKUP)
        self.assertEqual(len(section_at(result, "10.4")["additional_subsections"]), 1)


if __name__ == "__main__":
    unittest.main()
