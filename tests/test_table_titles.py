import json
import unittest

import paths  # noqa: F401

from converter.generate.client import LlmError
from converter.generate.table_titles import title_untitled_tables

CUSTODY = {"origin": "v10_verbatim", "v10_sources": [], "actor": {"kind": "script", "name": "x"},
           "confidence": "high", "needs_review": False}


class FakeClient:
    name = "fake"
    model = "fake-model"

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def paragraph(text):
    return {"type": "paragraph", "text": text, "style": "Normal", "custody": CUSTODY}


def table(first_cell):
    return {"type": "table", "rows": [[first_cell, "Day 1"]], "custody": CUSTODY}


def protocol():
    return {"sections": {"1": {"number": "1.3", "title": "Schedule of Activities", "content": [
        paragraph("Table 1. Screening Visits"), table("Screening"),
        paragraph("Procedures after dosing are below."), table("Vital signs"),
    ]}}, "processing_log": []}


class TableTitleTests(unittest.TestCase):
    def test_only_uncaptioned_tables_are_sent_and_titled(self):
        document = protocol()
        client = FakeClient(json.dumps({"titles": {"1": "Table 2: Post-dose Procedures."}}))
        title_untitled_tables(document, client)
        captioned, untitled = document["sections"]["1"]["content"][1::2]
        self.assertNotIn("title", captioned)
        self.assertEqual(untitled["title"], "Post-dose Procedures")
        self.assertIn("1.3 Schedule of Activities", client.prompts[0])
        self.assertIn("Procedures after dosing are below.", client.prompts[0])
        self.assertNotIn("Screening |", client.prompts[0])

    def test_a_failed_call_leaves_tables_untitled_and_is_logged(self):
        document = protocol()
        title_untitled_tables(document, FakeClient(LlmError("down")))
        self.assertNotIn("title", document["sections"]["1"]["content"][3])
        self.assertEqual(document["processing_log"][-1]["summary"], "titled 0 of 1 uncaptioned table(s)")

    def test_no_call_when_every_table_has_a_caption(self):
        document = protocol()
        document["sections"]["1"]["content"][3]["title"] = "Post-dose Procedures"
        client = FakeClient("{}")
        title_untitled_tables(document, client)
        self.assertEqual(client.prompts, [])


if __name__ == "__main__":
    unittest.main()
