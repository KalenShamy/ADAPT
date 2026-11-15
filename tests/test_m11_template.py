import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from extract_m11_template import spec_records, template_sections, universal_text_by_section

REFERENCE = ROOT / "resources" / "reference" / "ich-m11"
TEMPLATE_HTML = REFERENCE / "m11_final_template_5.20.26.html"
SPEC_HTML = REFERENCE / "ICH_Step4_M11_Final_TechnicalSpecification_2025_1119.html"
GENERATED = ROOT / "resources" / "research" / "m11_template_sections.json"
ELEMENTS = ROOT / "resources" / "research" / "schema-inputs" / "m11_elements.json"
# the D/V/H markers of data elements; the spec's own key table and heading records have other values
DATA_RECORD = re.compile(r"D|V|D, V|V or D|Universal text and V, D")
REPEATING_SECTION_NOTE = re.compile(r"\s+where X.*")
NCIT_CODE = re.compile(r"C\d{5,6}")


def items_of(sections, number, kind):
    return [item for item in sections[number]["items"] if item["kind"] == kind]


class TemplateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = spec_records(SPEC_HTML.read_text())
        sections = template_sections(TEMPLATE_HTML.read_text(), universal_text_by_section(cls.records))
        cls.sections = {number: section.as_json() for number, section in sections.items()}

    def test_generated_file_is_current(self):
        self.assertEqual(json.loads(GENERATED.read_text()), self.sections)

    def test_heading_only_sections(self):
        for number in ("2", "7", "7.1", "9"):
            self.assertTrue(self.sections[number]["heading_only"], number)
        self.assertFalse(self.sections["6"]["heading_only"])

    def test_interventions_table_sits_under_6_and_points_to_6_1_and_6_9(self):
        table, = items_of(self.sections, "6", "table")
        self.assertEqual(table["header"][:3], ["Arm Name", "Arm Type", "Intervention Name"])
        self.assertEqual(table["header"][-1], "Sourcing")
        self.assertLessEqual({"6.1", "6.9"}, set(self.sections["6"]["cross_refs"]))
        self.assertEqual(items_of(self.sections, "6.1", "table"), [])

    def test_collection_procedures_labels_in_order(self):
        labels = [item["text"] for item in items_of(self.sections, "9.2.2", "label")]
        self.assertEqual(labels, ["Identification", "Severity", "Causality", "Recording", "Follow-up"])

    def test_prior_amendments_statements_and_tables(self):
        statements = [item["text"] for item in items_of(self.sections, "12.3", "optional_text")]
        self.assertIn("{This protocol has not been amended}.", statements)
        self.assertIn("{This is the first protocol amendment}.", statements)
        self.assertEqual(len(items_of(self.sections, "12.3", "table")), 2)

    def test_overall_design_fields_in_template_order(self):
        grid_labels = [label for table in items_of(self.sections, "1.1.2", "table") for label in table["labels"]]
        self.assertEqual(grid_labels[:4], ["Intervention:", "Population Type:", "Intervention Model:",
                                           "Population Diagnosis or Condition:"])
        self.assertIn("Stratification:", grid_labels)
        labels = [item["text"] for item in items_of(self.sections, "1.1.2", "label")]
        self.assertEqual(labels[:2], ["Number of Arms:", "Trial Blind Schema:"])
        self.assertEqual(labels[-1], "Committees")

    def test_fixed_text_comes_from_the_spec_universal_text(self):
        fixed = [item["text"] for item in items_of(self.sections, "5.2", "fixed")]
        self.assertEqual(fixed, ["To be eligible to participate in this trial, an individual must meet all the "
                                 "following criteria:"])

    def test_repeating_sections_use_x(self):
        self.assertIn("3.1.X", self.sections)
        self.assertIn("10.4.X.2", self.sections)
        self.assertIn("12.X", self.sections)
        self.assertNotIn("3.1.1", self.sections)

    def test_no_page_numbers_toc_lines_or_broken_lines(self):
        texts = [str(item.get("text", "")) for section in self.sections.values() for item in section["items"]]
        self.assertFalse([text for text in texts if re.fullmatch(r"\d+", text)])
        self.assertFalse([text for text in texts if "....." in text])
        broken = [text for text in texts if text[:1].islower() and not text.startswith(("o ", "or"))]
        self.assertEqual(broken, [])


class SpecElementsTests(unittest.TestCase):
    """m11_elements.json was parsed from the spec PDF; it must agree with the spec HTML the formats are read from."""

    @classmethod
    def setUpClass(cls):
        records = spec_records(SPEC_HTML.read_text())
        cls.data = [record for record in records if DATA_RECORD.fullmatch(record.get("dvh", ""))]
        cls.elements = json.loads(ELEMENTS.read_text())

    def section(self, text):
        return REPEATING_SECTION_NOTE.sub("", text.split(";")[0].strip()).lower()

    def test_same_number_of_data_elements(self):
        # the PDF marks two data elements as headings; m11_elements.json flags them as anomalies
        expected = [element for element in self.elements if not element["pdf_dvh_anomaly"]]
        self.assertEqual(len(self.data), len(expected))

    def test_same_sections(self):
        self.assertEqual({self.section(record.get("section", "")) for record in self.data},
                         {self.section(element["m11_section"]) for element in self.elements})

    def test_same_codes(self):
        html_codes = {code for record in self.data for code in NCIT_CODE.findall(record.get("definition", ""))}
        element_codes = {code for element in self.elements if not element["pdf_dvh_anomaly"]
                         for code in element["ncit_codes"]}
        self.assertEqual(element_codes - html_codes, set())


if __name__ == "__main__":
    unittest.main()
