import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from converter.generate.fields import interventions_extraction, safety_reporting_extraction, extract_country_regions, extract_objectives, extract, fields_description, navigate, overall_design_extraction, section_at, typed_value
from converter.render.docx import filled_text, label_text
from converter.validate import load_schema, validate_document
from minimal_instance_builder import minimal_instance  # noqa: E402  (schema/ is on the path via converter.validate)


class FakeClient:
    name = "fake"
    model = "fake"

    def __init__(self, answer):
        self.answer = answer

    def complete(self, prompt):
        return json.dumps(self.answer)


class SequenceClient(FakeClient):
    """Gives each reply in turn and records the prompts."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return json.dumps(self.answers.pop(0))


def protocol_with_synopsis(schema):
    protocol = minimal_instance(schema)
    section = section_at(protocol, "1.1.2")
    source = {"section_id": "v10:1.1", "found_number": "1.1", "found_heading": "Synopsis", "match": "exact",
              "source_indices": [0], "portion": "all"}
    custody = {"origin": "v10_derived", "v10_sources": [source], "actor": {"kind": "script", "name": "t"},
               "confidence": "high", "needs_review": False}
    section["status"] = "populated"
    section["custody"] = custody
    section["content"] = [{"type": "paragraph", "text": "Randomized, double-blind, placebo-controlled study.",
                           "style": "Normal", "source_index": 0, "custody": custody}]
    return protocol


class OverallDesignTests(unittest.TestCase):
    schema = load_schema()

    def validate(self, protocol):
        return validate_document(protocol)[0]

    def test_valid_answers_fill_slots_and_invalid_ones_stay_missing(self):
        protocol = protocol_with_synopsis(self.schema)
        client = FakeClient({
            "trial_blind_schema": "Double Blind",
            "control_type": "Placebo",
            "number_of_arms": 2,
            "blinded_roles": ["Participant", "Investigator", "Wizard"],
            "intervention_duration": {"quantity": 90, "unit": "Days"},
            "stratification": "Maybe",
            "master_protocol": None,
        })
        result = extract(protocol, client, self.schema, self.validate, overall_design_extraction(self.schema))
        fields = section_at(result, "1.1.2")["fields"]
        self.assertEqual(fields["trial_blind_schema"]["value"]["decode"], "Double Blind")
        self.assertEqual(fields["trial_blind_schema"]["status"], "filled")
        self.assertEqual(fields["trial_blind_schema"]["custody"]["origin"], "ai_transformed")
        self.assertTrue(fields["trial_blind_schema"]["custody"]["needs_review"])
        self.assertEqual(fields["number_of_arms"]["value"], 2)
        self.assertEqual(filled_text(fields, ["intervention_duration"]), "90 Days")
        self.assertEqual([role["value"]["decode"] for role in fields["blinded_roles"]], ["Participant", "Investigator"])
        self.assertEqual(fields["stratification"]["status"], "missing")
        self.assertEqual(fields["master_protocol"]["status"], "missing")
        self.assertEqual(self.validate(result), [])

    def test_each_answer_keeps_the_confidence_the_model_gave_it(self):
        protocol = protocol_with_synopsis(self.schema)
        client = FakeClient({"trial_blind_schema": {"value": "Double Blind", "confidence": "high"},
                             "number_of_arms": {"value": 2, "confidence": "low"}})
        result = extract(protocol, client, self.schema, self.validate, overall_design_extraction(self.schema))
        fields = section_at(result, "1.1.2")["fields"]
        self.assertEqual(fields["trial_blind_schema"]["custody"]["confidence"], "high")
        self.assertEqual(fields["number_of_arms"]["value"], 2)
        self.assertEqual(fields["number_of_arms"]["custody"]["confidence"], "low")

    def test_a_row_list_wrapped_like_a_single_field_still_fills(self):
        row = {"arm_name": "Arm A", "intervention_name": "Drug A", "confidence": "low"}
        client = FakeClient({"interventions": {"value": [row], "confidence": "high"}})
        result = extract(protocol_with_synopsis(self.schema), client, self.schema, self.validate,
                         interventions_extraction(self.schema))
        filled, = section_at(result, "6")["fields"]["interventions"]
        self.assertEqual(filled["intervention_name"]["value"], "Drug A")
        self.assertEqual(filled["intervention_name"]["custody"]["confidence"], "low")

    def test_a_row_that_only_says_nothing_applies_is_left_out(self):
        real = {"event_type": "Adverse Event", "situational_scope": "All AEs", "confidence": "high"}
        junk = {"event_type": "Drug/Device Combination Product Complaint",
                "situational_scope": "not applicable; no medical device is used",
                "reporting_method": "not applicable", "confidence": "medium"}
        client = FakeClient({"safety_event_reporting": [real, junk]})
        result = extract(protocol_with_synopsis(self.schema), client, self.schema, self.validate,
                         safety_reporting_extraction(self.schema))
        row, = section_at(result, "9.2")["fields"]["safety_event_reporting"]
        self.assertEqual(row["situational_scope"]["value"], "All AEs")

    def test_a_row_field_without_a_quote_from_the_source_is_flagged(self):
        row = {"event_type": "Serious Adverse Event", "situational_scope": "All SAEs",
               "backup_reporting_method": "Telephone notification to the SAE coordinator", "confidence": "high",
               "evidence": {"situational_scope": "Randomized,  DOUBLE-BLIND, placebo-controlled study.",
                            "backup_reporting_method": "call the SAE coordinator"}}
        client = FakeClient({"safety_event_reporting": [row]})
        result = extract(protocol_with_synopsis(self.schema), client, self.schema, self.validate,
                         safety_reporting_extraction(self.schema))
        filled, = section_at(result, "9.2")["fields"]["safety_event_reporting"]
        self.assertEqual(filled["situational_scope"]["custody"]["confidence"], "high")
        self.assertEqual(filled["backup_reporting_method"]["custody"]["confidence"], "very_low")
        self.assertIn("note", filled["backup_reporting_method"]["custody"])
        self.assertEqual(self.validate(result), [])

    def test_a_short_quote_that_is_the_value_itself_supports_it(self):
        row = {"arm_name": "Arm A", "intervention_name": "placebo", "confidence": "high",
               "evidence": {"arm_name": "Arm A", "intervention_name": "placebo"}}
        client = FakeClient({"interventions": [row]})
        result = extract(protocol_with_synopsis(self.schema), client, self.schema, self.validate,
                         interventions_extraction(self.schema))
        filled, = section_at(result, "6")["fields"]["interventions"]
        self.assertEqual(filled["intervention_name"]["custody"]["confidence"], "high")
        self.assertEqual(filled["arm_name"]["custody"]["confidence"], "very_low")

    def protocol_mentioning_pregnancy(self):
        protocol = protocol_with_synopsis(self.schema)
        section_at(protocol, "1.1.2")["content"][0]["text"] += (
            " Pregnancies are reported within 24 hours of learning of the pregnancy, until 6 months after the last dose.")
        return protocol

    def extract_safety(self, protocol, replies):
        client = SequenceClient(replies)
        result = extract(protocol, client, self.schema, self.validate, safety_reporting_extraction(self.schema))
        rows = section_at(result, "9.2")["fields"]["safety_event_reporting"]
        return rows, client.prompts

    AE_ROW = {"event_type": "Adverse Event", "situational_scope": "All AEs", "confidence": "high"}

    def test_a_left_out_row_the_source_mentions_is_asked_for_again(self):
        pregnancy = {"event_type": "Pregnancy Event", "timing_for_reporting": "Within 24 hours",
                     "reportable_period_end": "6 months after the last dose", "confidence": "high",
                     "evidence": {"timing_for_reporting": "reported within 24 hours of learning",
                                  "reportable_period_end": "until 6 months after the last dose"}}
        rows, prompts = self.extract_safety(self.protocol_mentioning_pregnancy(), [
            {"safety_event_reporting": [self.AE_ROW]}, {"safety_event_reporting": [pregnancy]}])
        self.assertEqual(len(prompts), 2)
        self.assertIn("only the row for Pregnancy Event", prompts[1])
        self.assertEqual([row["event_type"]["value"]["decode"] for row in rows], ["Adverse Event", "Pregnancy Event"])

    def test_a_follow_up_row_without_quotes_for_its_timing_is_dropped(self):
        invented = {"event_type": "Pregnancy Event", "timing_for_reporting": "Within 24 hours",
                    "reportable_period_end": "30 days after the last dose", "confidence": "high",
                    "evidence": {"timing_for_reporting": "reported within 24 hours of learning"}}
        rows, _ = self.extract_safety(self.protocol_mentioning_pregnancy(), [
            {"safety_event_reporting": [self.AE_ROW]}, {"safety_event_reporting": [invented]}])
        self.assertEqual([row["event_type"]["value"]["decode"] for row in rows], ["Adverse Event"])

    def test_a_field_the_model_says_it_guessed_is_flagged(self):
        row = {"event_type": "Adverse Event", "situational_scope": "All AEs", "backup_reporting_method": "Paper form",
               "confidence": "high", "not_in_source": ["backup_reporting_method"],
               "evidence": {"situational_scope": "Randomized, double-blind, placebo-controlled study."}}
        rows, _ = self.extract_safety(protocol_with_synopsis(self.schema), [{"safety_event_reporting": [row]}])
        self.assertEqual(rows[0]["situational_scope"]["custody"]["confidence"], "high")
        guessed = rows[0]["backup_reporting_method"]["custody"]
        self.assertEqual(guessed["confidence"], "very_low")
        self.assertIn("does not state", guessed["note"])

    def test_a_follow_up_row_that_guessed_its_timing_is_dropped(self):
        guessed = {"event_type": "Pregnancy Event", "timing_for_reporting": "Within 24 hours",
                   "reportable_period_end": "6 months after the last dose", "confidence": "high",
                   "not_in_source": ["reportable_period_end"],
                   "evidence": {"timing_for_reporting": "reported within 24 hours of learning",
                                "reportable_period_end": "until 6 months after the last dose"}}
        rows, _ = self.extract_safety(self.protocol_mentioning_pregnancy(), [
            {"safety_event_reporting": [self.AE_ROW]}, {"safety_event_reporting": [guessed]}])
        self.assertEqual([row["event_type"]["value"]["decode"] for row in rows], ["Adverse Event"])

    def test_no_follow_up_when_the_source_never_mentions_the_row(self):
        _, prompts = self.extract_safety(protocol_with_synopsis(self.schema), [{"safety_event_reporting": [self.AE_ROW]}])
        self.assertEqual(len(prompts), 1)

    def test_not_applicable_text_answer_is_a_not_applicable_slot(self):
        protocol = protocol_with_synopsis(self.schema)
        client = FakeClient({"control_description": "not applicable; the trial has no control"})
        result = extract(protocol, client, self.schema, self.validate, overall_design_extraction(self.schema))
        slot = section_at(result, "1.1.2")["fields"]["control_description"]
        self.assertEqual((slot["status"], slot["value"]), ("not_applicable", None))

    def test_age_limits_fill_and_no_upper_limit_reads_not_applicable(self):
        protocol = protocol_with_synopsis(self.schema)
        client = FakeClient({
            "population_age.minimum": 18, "population_age.minimum_unit": "Years",
            "population_age.maximum": "not applicable", "population_age.maximum_unit": None,
        })
        result = extract(protocol, client, self.schema, self.validate, overall_design_extraction(self.schema))
        fields = section_at(result, "1.1.2")["fields"]
        self.assertEqual(label_text("Minimum:", fields), "18 Years")
        self.assertEqual(fields["population_age"]["maximum"]["status"], "not_applicable")
        self.assertEqual(label_text("Maximum:", fields), "Not applicable")
        self.assertEqual(self.validate(result), [])

    def test_unsupported_values_are_rejected(self):
        description = navigate(fields_description(self.schema, "Fields_1_1_2"), "number_of_arms")
        self.assertIsNone(typed_value(description, True))
        self.assertIsNone(typed_value(description, -1))
        self.assertIsNone(typed_value(description, "2"))

    def test_nothing_is_asked_when_the_section_has_no_source(self):
        protocol = minimal_instance(self.schema)
        result = extract(protocol, FakeClient({}), self.schema, self.validate, overall_design_extraction(self.schema))
        self.assertIn("filled 0", result["processing_log"][-1]["summary"])

    def test_country_regions_fill_an_empty_12_2_and_reject_bad_codes(self):
        protocol = protocol_with_synopsis(self.schema)
        client = FakeClient({"country_regions": [
            {"country_code": "DEU", "country_name": "Germany", "requirements": ["Use the German ICF."], "clarifications": []},
            {"country_code": "Germany", "country_name": "Germany", "requirements": ["x"], "clarifications": []},
        ]})
        result = extract_country_regions(protocol, client, self.schema, self.validate)
        section = section_at(result, "12.2")
        self.assertEqual([r["identifier"]["value"]["code"] for r in section["fields"]["country_regions"]], ["DEU"])
        self.assertEqual(section["status"], "populated")
        self.assertEqual(self.validate(result), [])

    def test_objectives_fill_section_3_and_replace_its_source_headings(self):
        protocol = protocol_with_synopsis(self.schema)
        client = FakeClient({
            "primary": [{"category": "Efficacy", "statement": "To compare drug A with placebo.",
                         "estimands": [{"endpoint": "Time to first event", "population": None,
                                        "intercurrent_events": [{"description": "Rescue", "strategy": "Treatment policy"}]}]}],
            "secondary": [{"statement": ""}],
            "exploratory": [],
        })
        result = extract_objectives(protocol, client, self.schema, self.validate)
        primary = section_at(result, "3.1")["objectives"]
        self.assertEqual(primary[0]["id"], "primary-1")
        self.assertEqual(primary[0]["estimands"][0]["endpoint"]["value"], "Time to first event")
        self.assertEqual(primary[0]["estimands"][0]["intercurrent_events"][0]["strategy"]["value"], "Treatment policy")
        self.assertEqual(section_at(result, "3.2")["status"], "not_applicable")
        self.assertEqual(self.validate(result), [])


if __name__ == "__main__":
    unittest.main()
