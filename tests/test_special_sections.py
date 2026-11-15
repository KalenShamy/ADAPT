import unittest

import paths  # noqa: F401

from converter.model import Heading, Paragraph, Span, Subheading, Table
from converter.special_sections import split_objectives, table_provenance


def cell(text, colspan=1, rowspan=1):
    return {"rowspan": rowspan, "colspan": colspan, "fill": None,
            "paragraphs": [Paragraph([Span(text, True, False, False, 10)], page=4)]}


def objectives_table():
    return Table([[cell("Objectives"), cell("Endpoints")],
                  [cell("Primary", colspan=2), None], [cell("First objective"), cell("First endpoint")],
                  [cell("Secondary", colspan=2), None], [cell("Second objective"), cell("Second endpoint")],
                  [cell("Exploratory", colspan=2), None], [cell("Third objective"), cell("Third endpoint")]],
                 [200, 200], 4, 4, (5, 2), 300)


def text(content, kind="body"):
    return Paragraph([Span(content, False, False, False, 10)], kind=kind)


class ObjectivesTests(unittest.TestCase):
    def test_all_three_objective_parts_keep_source_provenance(self):
        table = objectives_table()
        result = split_objectives([table])
        self.assertTrue(result.split)
        self.assertEqual([len(result.primary), len(result.secondary), len(result.exploratory)], [1, 1, 1])
        pieces = result.primary + result.secondary + result.exploratory
        self.assertEqual([len(piece.grid) for piece in pieces], [3, 3, 3])
        self.assertEqual(table_provenance([Heading("3", "Objectives", 4), table], pieces)["outputs"], [[0], [0], [0]])

    def test_rowspan_prevents_unsafe_split(self):
        table = Table([[cell("Objectives", rowspan=2), cell("Endpoints")],
                       [None, cell("Secondary")]], [200, 200], 4, 4, (5, 2), 100)
        result = split_objectives([table])
        self.assertFalse(result.split)
        self.assertEqual(result.primary, [table])

    def test_estimand_subheadings_go_to_the_objective_they_name(self):
        primary_estimand = text("The primary estimand is PFS.")
        secondary_estimand = text("The key secondary estimand is OS.")
        result = split_objectives([
            objectives_table(),
            Subheading("Estimand(s) for Primary Objective(s)", "bold"), primary_estimand,
            Subheading("Supplementary Estimand(s)", "label"),
            Subheading("Estimands for Secondary Objective(s)", "bold"), secondary_estimand,
        ])
        self.assertIn(primary_estimand, result.primary)
        self.assertEqual(result.primary[-1].title, "Supplementary Estimand(s)")
        self.assertIn(secondary_estimand, result.secondary)
        self.assertEqual(len(result.exploratory), 1)

    def test_table_caption_goes_with_the_first_piece(self):
        caption = text("Table 5. Objectives and Endpoints", kind="caption")
        result = split_objectives([caption, objectives_table()])
        self.assertEqual(result.preamble, [])
        self.assertIs(result.primary[0], caption)


if __name__ == "__main__":
    unittest.main()
