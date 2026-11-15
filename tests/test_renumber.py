import unittest
from pathlib import Path

import paths  # noqa: F401

from converter.cli import DEFAULT_TEMPLATE
from converter.render.docx import UNSPLIT_TEXT, DocxRenderer
from converter.render.renumber import displayed_numbers, renumber_text


def section(number, status="populated", **children):
    return {"number": number, "status": status, "subsections": children}


class DisplayedNumberTests(unittest.TestCase):
    def test_siblings_after_an_omitted_section_shift_up(self):
        sections = {"1": section("1", **{
            "1.1": section("1.1", **{
                "1.1.1": section("1.1.1", "omitted"), "1.1.2": section("1.1.2"), "1.1.3": section("1.1.3")}),
        })}
        self.assertEqual(displayed_numbers(sections), {"1.1.2": "1.1.1", "1.1.3": "1.1.2"})

    def test_nothing_shifts_when_no_section_is_omitted(self):
        sections = {"1": section("1", **{"1.1": section("1.1"), "1.2": section("1.2")})}
        self.assertEqual(displayed_numbers(sections), {})

    def test_children_are_ordered_by_number_not_key(self):
        sections = {"1": section("1", **{
            "1.1": section("1.1"), "1.10": section("1.10"), "1.2": section("1.2", "omitted"), "1.9": section("1.9")})}
        self.assertEqual(displayed_numbers(sections), {"1.9": "1.2", "1.10": "1.3"})


class RenumberTextTests(unittest.TestCase):
    shifted = {"5.5.2": "5.5.1", "5.5.3": "5.5.2"}

    def test_section_references_follow_the_shift(self):
        self.assertEqual(renumber_text("See Section 5.5.2 and Sections 5.5.3, 5.4.", self.shifted),
                         "See Section 5.5.1 and Sections 5.5.2, 5.4.")

    def test_numbers_that_are_not_section_references_are_left_alone(self):
        text = "Dose 5.5.2 mg; Section 5.5.2 of the IB; Appendix 5.5.3"
        self.assertEqual(renumber_text(text, self.shifted), "Dose 5.5.2 mg; Section 5.5.2 of the IB; Appendix 5.5.3")


class HeadingLabelTests(unittest.TestCase):
    def test_labels_that_only_group_others_get_no_placeholder(self):
        renderer = DocxRenderer(DEFAULT_TEMPLATE, {}, Path("."), "")
        renderer.fixed_text("1.1.2", [], {})
        paragraphs = [paragraph.text for paragraph in renderer.writer.doc.paragraphs]
        for label in ("Duration:", "Committees:", "Population Age:"):
            self.assertNotEqual(paragraphs[paragraphs.index(label) + 1], UNSPLIT_TEXT)
        self.assertEqual(paragraphs[paragraphs.index("Stratification:") + 1], UNSPLIT_TEXT)


if __name__ == "__main__":
    unittest.main()
