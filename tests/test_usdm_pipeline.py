import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import docx
import pymupdf
from usdm4.api.wrapper import Wrapper

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from check_output import check_content, check_cross_references, check_outline, output_outline
from extract_to_usdm import enrich_title, extract_file, load_document
from pdf_to_cpt_docx import Document, Figure, FrontHeading, Heading, PageMark, Paragraph, Span
from restructure import template_outline
from write_v011_docx import DEFAULT_TEMPLATE, write_file


class PipelineTests(unittest.TestCase):
    def test_deep_headings_and_reference_lists(self):
        document = docx.Document()
        from docx.enum.style import WD_STYLE_TYPE
        for level in range(1, 7):
            name = f"0{level}Heading {level}"
            document.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
            document.add_paragraph(f"Level {level}", style=name)
        document.add_paragraph("Sections 1.1.1.1.1 and 1.1.1.1.1.1")
        self.assertEqual(len(output_outline(document)), 6)
        self.assertEqual(check_cross_references(document), [])
        document.add_paragraph("Sections 1.1 and 99.1")
        self.assertTrue(check_cross_references(document))

    def make_pdf(self, directory):
        path = Path(directory) / "protocol.pdf"
        with pymupdf.open() as pdf:
            page = pdf.new_page()
            page.insert_text((50, 50), "CLINICAL STUDY PROTOCOL\nProtocol title: Example study\n"
                             "Protocol number: ABC123\nStudy phase: Phase 3\n"
                             "Sponsor name: Example sponsor\nIND: 123456")
            pdf.save(path)
        return path

    def test_title_labels_do_not_capture_pattern_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            values = {}
            enrich_title(self.make_pdf(directory), values, [], [])
            self.assertEqual(values["title"], "Example study")
            self.assertEqual(values["protocol_number"], "ABC123")
            self.assertEqual(values["phase"], "Phase 3")
            self.assertEqual(values["sponsor"], "Example sponsor")
            self.assertEqual(values["regulatory_ids"], "IND: 123456")

    def test_json_alone_is_sufficient_and_content_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.make_pdf(directory)
            with pymupdf.open(path) as pdf:
                scanned_page = Figure(pdf[0].get_pixmap().tobytes("png"), pdf[0].rect.width, 4, True)
            paragraph = Paragraph([Span("Exact narrative & text", True, True, False, 11)], page=2)
            source = Document(front=[FrontHeading("Signature Page", 1), paragraph], body=[
                PageMark(False, 1), Heading("1", "Protocol Summary", 2),
                Heading("1.1", "Synopsis", 2), paragraph,
                Heading("2", "Introduction", 3), paragraph, scanned_page])
            with patch("extract_to_usdm.load_source", return_value=source), contextlib.redirect_stdout(io.StringIO()):
                json_path = extract_file(path, Path(directory))
            wrapper = Wrapper.model_validate_json(json_path.read_text())
            self.assertEqual(wrapper.study.versions[0].studyDesigns, [])
            self.assertTrue(wrapper.study.versions[0].studyIdentifiers)
            loaded, sidecar = load_document(json_path)
            self.assertEqual(loaded.front, source.front)
            self.assertEqual([block for block in loaded.body if not isinstance(block, PageMark)],
                             [block for block in source.body if not isinstance(block, PageMark)])
            path.unlink()
            with patch("pymupdf.open", side_effect=AssertionError("writer opened a PDF")), contextlib.redirect_stdout(io.StringIO()):
                output = write_file(json_path, Path(directory) / "written")
            audit = json.loads(output.with_suffix(".audit.json").read_text())
            document = docx.Document(output)
            self.assertEqual(len(document.inline_shapes), 1)
            section = document.sections[-1]
            self.assertLess(document.inline_shapes[0].height, section.page_height - section.top_margin - section.bottom_margin)
            self.assertEqual(check_content(document, audit), [])
            self.assertEqual(check_outline(document, template_outline(DEFAULT_TEMPLATE), audit)[0], [])
            for paragraph in document.paragraphs:
                if paragraph.text == "Exact narrative & text":
                    paragraph.text = "Tampered narrative"
                    break
            self.assertTrue(check_content(document, audit))


if __name__ == "__main__":
    unittest.main()
