import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "schema"))

from converter.export.fhir import (  # noqa: E402
    EXT_AMENDMENT, EXT_M11_STUDY, NOT_APPLICABLE_CODE, TARGET_ELEMENT, XHTML_NS, export_fhir, load_ig_terms,
    sidecar_paths,
)
from converter.render.docx import MISSING_TEXT  # noqa: E402
from converter.validate import load_schema  # noqa: E402
from test_protocol_schema import minimal_instance  # noqa: E402

TIMESTAMP = "2026-11-14T12:00:00+00:00"
DATA_ABSENT = "http://hl7.org/fhir/StructureDefinition/data-absent-reason"
ELEMENT_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")
# FHIR narrative XHTML elements (FHIR spec, Narrative)
NARRATIVE_TAGS = {
    "a", "abbr", "acronym", "b", "big", "blockquote", "br", "caption", "cite", "code", "col", "colgroup", "dd", "dfn",
    "div", "dl", "dt", "em", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "li", "ol", "p", "pre", "q", "samp",
    "small", "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "tt", "ul", "var",
}
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake"

SCHEMA = load_schema()
TERMS = load_ig_terms()
SCRIPT = {"kind": "script", "name": "test"}
LLM = {"kind": "llm", "name": "claude", "model": "test-model"}


def custody(origin="v10_derived", actor=SCRIPT, needs_review=False):
    return {"origin": origin, "v10_sources": [], "actor": actor, "confidence": "high", "needs_review": needs_review}


def filled(value):
    return {"value": value, "status": "filled", "custody": custody()}


def paragraph(text, origin="v10_verbatim", source_index=None):
    actor = LLM if origin.startswith("ai_") else SCRIPT
    block = {"type": "paragraph", "text": text, "style": "Normal",
             "custody": custody(origin, actor, needs_review=origin.startswith("ai_"))}
    if source_index is not None:
        block["source_index"] = source_index
    return block


def base_protocol():
    protocol = minimal_instance(SCHEMA)
    protocol["source"]["sha256"] = "0" * 64
    protocol["source"]["file_name"] = "sample.pdf"
    return protocol


def section(protocol, number):
    node = protocol["sections"][number.split(".")[0]]
    parts = number.split(".")
    for depth in range(2, len(parts) + 1):
        node = node["subsections"][".".join(parts[:depth])]
    return node


def resources(bundle, kind):
    return [entry["resource"] for entry in bundle["entry"] if entry["resource"]["resourceType"] == kind]


def walk_sections(sections):
    for item in sections:
        yield item
        yield from walk_sections(item.get("section", []))


def references(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "reference" and isinstance(item, str):
                yield item
            else:
                yield from references(item)
    elif isinstance(value, list):
        for item in value:
            yield from references(item)


def sub_extension(extension, url):
    return next(item for item in extension["extension"] if item["url"] == url)


def run(protocol, layout=None, assets_dir=None):
    return export_fhir(protocol, layout or {}, assets_dir or Path("unused.assets"), TIMESTAMP, TERMS, {})


class BundleShapeTest(unittest.TestCase):
    def setUp(self):
        self.bundle = run(base_protocol()).bundle

    def test_document_bundle_starts_with_composition(self):
        self.assertEqual(self.bundle["type"], "document")
        self.assertEqual(self.bundle["timestamp"], TIMESTAMP)
        self.assertTrue(self.bundle["identifier"]["value"].startswith("urn:uuid:"))
        self.assertEqual(self.bundle["entry"][0]["resource"]["resourceType"], "Composition")

    def test_every_reference_resolves_inside_the_bundle(self):
        full_urls = {entry["fullUrl"] for entry in self.bundle["entry"]}
        self.assertEqual(len(full_urls), len(self.bundle["entry"]))
        for reference in references(self.bundle["entry"]):
            self.assertIn(reference, full_urls)

    def test_same_input_and_timestamp_give_the_same_bundle(self):
        self.assertEqual(run(base_protocol()).bundle, self.bundle)

    def test_required_m11_extension_uses_data_absent_reason_when_empty(self):
        study = resources(self.bundle, "ResearchStudy")[0]
        m11 = next(item for item in study["extension"] if item["url"] == EXT_M11_STUDY)
        self.assertEqual(sub_extension(m11, "versionDate")["_valueDate"]["extension"][0]["url"], DATA_ABSENT)
        self.assertEqual(sub_extension(m11, "originalProtocol")["valueCodeableConcept"]["extension"][0]["url"], DATA_ABSENT)

    def test_identifier_is_present_even_without_values(self):
        study = resources(self.bundle, "ResearchStudy")[0]
        self.assertEqual(len(study["identifier"]), 1)
        self.assertEqual(study["identifier"][0]["type"]["coding"][0]["code"], "C132351")
        self.assertIn("_value", study["identifier"][0])

    def test_composition_author_is_an_organization(self):
        composition = self.bundle["entry"][0]["resource"]
        author = composition["author"][0]["reference"]
        kinds = {entry["fullUrl"]: entry["resource"]["resourceType"] for entry in self.bundle["entry"]}
        self.assertEqual(kinds[author], "Organization")


class TitlePageTest(unittest.TestCase):
    def setUp(self):
        protocol = base_protocol()
        page = protocol["title_page"]
        page["full_title"] = filled("A Phase 3 Trial")
        page["trial_acronym"] = filled("TRIAL")
        page["sponsor_protocol_identifier"] = filled("ABC-1")
        page["version_number"] = filled("2.0")
        page["version_date"] = filled("2026-01-31")
        page["original_protocol"] = filled({"code": "C49488", "decode": "Yes"})
        page["trial_phase"] = filled({"code": "C15602", "decode": "Phase 3"})
        page["sponsors"] = [{"role": "sponsor", "name": filled("Sponsor Inc."), "legal_address": filled("1 Road")}]
        page["regulatory_identifiers"] = [{"type": "nct_number", "label": "NCT", "id": filled("NCT00000001")}]
        page["investigational_product_names"] = [{"kind": "nonproprietary", "name": filled("examplumab")}]
        self.bundle = run(protocol).bundle
        self.study = resources(self.bundle, "ResearchStudy")[0]

    def test_title_page_values_reach_research_study(self):
        self.assertEqual(self.study["title"], "A Phase 3 Trial")
        self.assertEqual(self.study["version"], "2.0")
        self.assertEqual([label["value"] for label in self.study["label"]], ["TRIAL"])
        self.assertEqual(self.study["phase"]["coding"][0]["display"], "Phase III Trial")
        codes = {identifier["type"]["coding"][0]["code"]: identifier["value"] for identifier in self.study["identifier"]}
        self.assertEqual(codes, {"C132351": "ABC-1", "C172240": "NCT00000001"})

    def test_original_protocol_yes_becomes_not_amended(self):
        m11 = next(item for item in self.study["extension"] if item["url"] == EXT_M11_STUDY)
        self.assertEqual(sub_extension(m11, "originalProtocol")["valueCodeableConcept"]["coding"][0]["code"], "C218485")
        self.assertEqual(sub_extension(m11, "versionDate")["valueDate"], "2026-01-31")
        self.assertFalse(any(item["url"] == EXT_AMENDMENT for item in self.study["extension"]))

    def test_sponsor_is_party_and_author(self):
        organization = resources(self.bundle, "Organization")[0]
        self.assertEqual(organization["name"], "Sponsor Inc.")
        self.assertEqual(organization["contact"][0]["address"]["text"], "1 Road")
        self.assertEqual(self.study["associatedParty"][0]["role"]["coding"][0]["code"], "C70793")
        composition = self.bundle["entry"][0]["resource"]
        self.assertEqual(composition["author"][0]["reference"], self.study["associatedParty"][0]["party"]["reference"])

    def test_device_manufacturer_uses_ncit_display(self):
        protocol = base_protocol()
        protocol["title_page"]["device_manufacturers"] = [{"name": filled("Device Co"), "legal_address": filled("4 Way")}]
        study = resources(run(protocol).bundle, "ResearchStudy")[0]
        coding = study["associatedParty"][-1]["role"]["coding"][0]
        self.assertEqual((coding["code"], coding["display"]), ("C156625", "Device Manufacture"))

    def test_product_name_becomes_medicinal_product(self):
        product = resources(self.bundle, "MedicinalProductDefinition")[0]
        self.assertEqual(product["name"][0]["productName"], "examplumab")
        coding = product["name"][0]["type"]["coding"][0]
        self.assertEqual((coding["code"], coding["display"]), ("C97054", "Generic Name"))

    def test_unsure_value_is_exported_as_fact(self):
        protocol = base_protocol()
        protocol["title_page"]["version_date"] = {
            "value": "2026-02-01", "status": "unsure", "custody": custody(needs_review=True)}
        study = resources(run(protocol).bundle, "ResearchStudy")[0]
        m11 = next(item for item in study["extension"] if item["url"] == EXT_M11_STUDY)
        self.assertEqual(sub_extension(m11, "versionDate")["valueDate"], "2026-02-01")

    def test_identifier_system_comes_from_config(self):
        bundle = export_fhir(copy.deepcopy(base_protocol()), {}, Path("unused"), TIMESTAMP, TERMS,
                             {"sponsor_protocol": "https://sponsor.example/ids"}).bundle
        study = resources(bundle, "ResearchStudy")[0]
        self.assertEqual(study["identifier"][0]["system"], "https://sponsor.example/ids")


class AmendmentTest(unittest.TestCase):
    def test_amendment_extension_needs_identifier_and_scope(self):
        protocol = base_protocol()
        page = protocol["title_page"]
        page["original_protocol"] = filled({"code": "C49487", "decode": "No"})
        page["amendment_identifier"] = filled("Amendment 2")
        page["amendment_scope"] = {"value": None, "status": "not_applicable", "custody": custody()}
        protocol["amendment_details"]["summary"] = filled("Changed dosing.")
        study = resources(run(protocol).bundle, "ResearchStudy")[0]
        amendment = next(item for item in study["extension"] if item["url"] == EXT_AMENDMENT)
        self.assertEqual(sub_extension(amendment, "identifier")["valueIdentifier"]["value"], "Amendment 2")
        self.assertEqual(sub_extension(amendment, "scope")["valueCodeableConcept"]["coding"][0]["code"], NOT_APPLICABLE_CODE)
        self.assertEqual(sub_extension(amendment, "summary")["valueString"], "Changed dosing.")


class CompositionTest(unittest.TestCase):
    def setUp(self):
        self.protocol = base_protocol()
        target = section(self.protocol, "2.1")
        target["status"] = "populated"
        target["content"] = [paragraph("Why we run this trial.")]
        target["custody"] = custody("v10_verbatim")
        self.bundle = run(self.protocol).bundle
        self.composition = self.bundle["entry"][0]["resource"]
        self.sections = list(walk_sections(self.composition["section"]))

    def test_sections_carry_ig_codes(self):
        by_title = {item["title"]: item for item in self.sections}
        purpose = next(item for title, item in by_title.items() if title.startswith("2.1 "))
        self.assertEqual(purpose["code"]["coding"][0]["code"], TERMS.section_codes["2.1"])
        self.assertIn("Why we run this trial.", purpose["text"]["div"])
        codes = {item["code"]["coding"][0]["code"] for item in self.sections if "code" in item}
        self.assertTrue(codes <= set(TERMS.section_codes.values()))

    def test_every_section_has_text_or_children_and_a_valid_unique_id(self):
        ids = [item["id"] for item in self.sections]
        self.assertEqual(len(ids), len(set(ids)))
        for item in self.sections:
            self.assertRegex(item["id"], ELEMENT_ID)
            self.assertTrue("text" in item or item.get("section"), item["title"])

    def test_missing_section_has_marker_and_empty_reason(self):
        missing = [item for item in self.sections if "emptyReason" in item]
        self.assertTrue(missing)
        self.assertIn(MISSING_TEXT, missing[0]["text"]["div"])
        self.assertNotIn("entry", missing[0])

    def test_omitted_sections_are_left_out(self):
        protocol = copy.deepcopy(self.protocol)
        section(protocol, "2.1")["status"] = "omitted"
        section(protocol, "2.1")["content"] = []
        titles = [item["title"] for item in walk_sections(run(protocol).bundle["entry"][0]["resource"]["section"])]
        self.assertFalse(any(title.startswith("2.1 ") for title in titles))

    def test_narrative_is_fhir_xhtml(self):
        for item in self.sections:
            if "text" in item:
                assert_fhir_xhtml(self, item["text"]["div"])

    def test_no_ai_content_means_final(self):
        self.assertEqual(self.composition["status"], "final")
        self.assertEqual(resources(self.bundle, "ResearchStudy")[0]["status"], "active")
        self.assertEqual(resources(self.bundle, "Provenance"), [])


class LayoutTest(unittest.TestCase):
    def test_layout_xhtml_is_cleaned_and_images_are_embedded(self):
        with tempfile.TemporaryDirectory() as folder:
            assets = Path(folder) / "sample.assets"
            assets.mkdir()
            (assets / "fig-1.png").write_bytes(PNG_BYTES)
            layout = {"7": (
                '<p data-kind="&quot;body&quot;"><span data-size="10"><strong>Bold</strong> text</span></p>'
                '<span class="page-mark" data-page="3"></span>'
                '<table data-columns="1"><colgroup><col style="width:10pt" /></colgroup>'
                '<tr><th rowspan="1" colspan="1" data-cell="{}"><p>Head</p></th></tr></table>'
                '<img src="sample.assets/fig-1.png" alt="Source figure" data-page="3" />'
            )}
            protocol = base_protocol()
            target = section(protocol, "2.1")
            target["status"] = "populated"
            target["content"] = [paragraph("Bold text", source_index=7)]
            bundle = run(protocol, layout, assets).bundle
        purpose = next(item for item in walk_sections(bundle["entry"][0]["resource"]["section"])
                       if item["title"].startswith("2.1 "))
        div = purpose["text"]["div"]
        assert_fhir_xhtml(self, div)
        self.assertIn("<strong>Bold</strong>", div)
        self.assertIn('src="data:image/png;base64,', div)
        self.assertNotIn("page-mark", div)

    def test_missing_figure_fails_loudly(self):
        protocol = base_protocol()
        target = section(protocol, "2.1")
        target["status"] = "populated"
        target["content"] = [paragraph("x", source_index=1)]
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(FileNotFoundError):
            run(protocol, {"1": '<img src="a.assets/none.png" />'}, Path(folder))


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        protocol = base_protocol()
        target = section(protocol, "2.1")
        target["status"] = "populated"
        target["content"] = [paragraph("Drafted purpose.", origin="ai_generated")]
        target["custody"] = custody("ai_generated", LLM, needs_review=True)
        protocol["processing_log"].append({"step": "generate", "actor": LLM, "at": "2026-11-14T11:00:00+00:00",
                                           "summary": "drafted 1 section"})
        self.bundle = run(protocol).bundle
        self.composition = self.bundle["entry"][0]["resource"]

    def test_ai_section_gets_provenance_pointing_at_it(self):
        provenances = resources(self.bundle, "Provenance")
        self.assertEqual(len(provenances), 1)
        target = provenances[0]["target"][0]
        self.assertEqual(target["reference"], self.bundle["entry"][0]["fullUrl"])
        element = next(item["valueUri"] for item in target["extension"] if item["url"] == TARGET_ELEMENT)
        drafted = next(item for item in walk_sections(self.composition["section"]) if item["id"] == element)
        self.assertTrue(drafted["title"].startswith("2.1 "))
        self.assertEqual(provenances[0]["activity"]["coding"][0]["code"], "CREATE")
        self.assertEqual(provenances[0]["recorded"], "2026-11-14T11:00:00+00:00")

    def test_model_is_the_agent(self):
        device = resources(self.bundle, "Device")[0]
        self.assertEqual(device["name"][0]["value"], "claude (test-model)")
        self.assertEqual(device["name"][0]["type"]["coding"][0]["code"], "user-friendly-name")

    def test_ai_text_is_exported_as_fact(self):
        drafted = next(item for item in walk_sections(self.composition["section"]) if item["title"].startswith("2.1 "))
        self.assertIn("Drafted purpose.", drafted["text"]["div"])
        self.assertNotIn("Review", drafted["text"]["div"])
        self.assertEqual(self.composition["status"], "final")
        self.assertEqual(resources(self.bundle, "ResearchStudy")[0]["status"], "active")


class LowConfidenceTest(unittest.TestCase):
    def protocol_with_draft(self, confidence):
        protocol = base_protocol()
        target = section(protocol, "2.1")
        target["status"] = "populated"
        block = paragraph("Unsure purpose.", origin="ai_generated")
        block["custody"]["confidence"] = confidence
        target["content"] = [block]
        return protocol

    def drafted(self, bundle):
        return next(item for item in walk_sections(bundle["entry"][0]["resource"]["section"])
                    if item["title"].startswith("2.1 "))

    def test_low_confidence_text_is_withheld_and_marked(self):
        protocol = self.protocol_with_draft("low")
        before = copy.deepcopy(protocol)
        bundle = run(protocol).bundle
        self.assertEqual(protocol, before)
        self.assertNotIn("Unsure purpose.", json.dumps(bundle))
        drafted = self.drafted(bundle)
        self.assertIn("Needs human input", drafted["text"]["div"])
        assert_fhir_xhtml(self, drafted["text"]["div"])
        self.assertEqual(resources(bundle, "Provenance"), [])
        task = resources(bundle, "Task")[0]
        self.assertEqual((task["status"], task["intent"]), ("requested", "order"))
        self.assertEqual(task["focus"]["reference"], bundle["entry"][0]["fullUrl"])
        self.assertEqual(task["focus"]["extension"][0]["valueUri"], drafted["id"])
        self.assertIn("section text", task["description"])
        self.assertEqual(bundle["entry"][0]["resource"]["status"], "preliminary")
        self.assertEqual(resources(bundle, "ResearchStudy")[0]["status"], "draft")

    def test_very_low_confidence_text_is_withheld(self):
        bundle = run(self.protocol_with_draft("very_low")).bundle
        self.assertNotIn("Unsure purpose.", json.dumps(bundle))
        self.assertEqual(len(resources(bundle, "Task")), 1)

    def test_medium_confidence_text_is_exported(self):
        bundle = run(self.protocol_with_draft("medium")).bundle
        self.assertIn("Unsure purpose.", self.drafted(bundle)["text"]["div"])
        self.assertEqual(resources(bundle, "Task"), [])
        self.assertEqual(bundle["entry"][0]["resource"]["status"], "final")

    def test_low_confidence_title_page_value_is_withheld(self):
        protocol = base_protocol()
        slot = filled("2026-03-01")
        slot["custody"]["confidence"] = "low"
        slot["custody"]["needs_review"] = True
        protocol["title_page"]["version_date"] = slot
        bundle = run(protocol).bundle
        self.assertNotIn("2026-03-01", json.dumps(bundle))
        study = resources(bundle, "ResearchStudy")[0]
        m11 = next(item for item in study["extension"] if item["url"] == EXT_M11_STUDY)
        reason = sub_extension(m11, "versionDate")["_valueDate"]["extension"][0]
        self.assertEqual((reason["url"], reason["valueCode"]), (DATA_ABSENT, "temp-unknown"))
        task = resources(bundle, "Task")[0]
        self.assertEqual(task["focus"]["reference"], bundle["entry"][1]["fullUrl"])
        self.assertIn("Version date", task["description"])


class ConformanceTest(unittest.TestCase):
    def amendment(self, protocol):
        protocol["title_page"]["amendment_identifier"] = filled("Amendment 1")
        exported = run(protocol)
        study = resources(exported.bundle, "ResearchStudy")[0]
        return next(item for item in study["extension"] if item["url"] == EXT_AMENDMENT), exported.notes

    def test_missing_required_code_is_reported_as_not_conformant(self):
        notes = run(base_protocol()).notes
        self.assertTrue(any("NOT CONFORMANT: originalProtocol" in note for note in notes), notes)

    def test_enrolled_number_is_positive_int_only_for_whole_numbers(self):
        for quantity, expected in ((12, {"valuePositiveInt": 12}), (0.5, {"valueQuantity": {"value": 0.5}}),
                                   (0, {"valueQuantity": {"value": 0}})):
            protocol = base_protocol()
            protocol["amendment_details"]["approximate_enrolled"]["quantity"] = filled(quantity)
            amendment, notes = self.amendment(protocol)
            impact = next(item for item in amendment["extension"] if item["url"].endswith("m11-amendment-scope-impact"))
            number = sub_extension(impact, "number")
            self.assertEqual({key: value for key, value in number.items() if key != "url"}, expected)
            self.assertTrue(any("NOT CONFORMANT: amendment scope impact scope" in note for note in notes), notes)

    def test_task_and_device_use_the_ballot3_code_systems(self):
        protocol = base_protocol()
        slot = filled("2026-03-01")
        slot["custody"]["confidence"] = "low"
        protocol["title_page"]["version_date"] = slot
        node = section(protocol, "2.1")
        node["status"] = "populated"
        node["content"] = [paragraph("Drafted purpose.", "ai_generated")]
        bundle = run(protocol).bundle
        task, device = resources(bundle, "Task")[0], resources(bundle, "Device")[0]
        self.assertEqual(task["code"]["coding"][0]["system"], "http://hl7.org/fhir/CodeSystem/task-code")
        self.assertEqual(device["name"][0]["type"]["coding"][0]["system"], "http://hl7.org/fhir/device-nametype")
        for resource in (task, device, bundle["entry"][0]["resource"]):
            assert_fhir_xhtml(self, resource["text"]["div"])

    def test_every_objective_gets_the_objective_section_code(self):
        protocol = base_protocol()
        objectives = section(protocol, "3.1")["objectives"]
        second = copy.deepcopy(objectives[0])
        second.update(id="primary-2", number="3.1.2", title="Primary Objective 2")
        objectives.append(second)
        composition = run(protocol).bundle["entry"][0]["resource"]
        codes = [item["code"]["coding"][0]["code"] for item in walk_sections(composition["section"])
                 if item["title"].startswith("3.1.") and "code" in item]
        self.assertEqual(codes, [TERMS.section_codes["3.1.1"]] * 2)

    def test_empty_reason_only_on_sections_without_children(self):
        composition = run(base_protocol()).bundle["entry"][0]["resource"]
        for item in walk_sections(composition["section"]):
            self.assertFalse("emptyReason" in item and "section" in item, item["title"])

    def test_typed_signature_is_not_the_signature_method(self):
        protocol = base_protocol()
        protocol["title_page"]["sponsor_signatories"] = [{
            "name": filled("Dr X"), "title": filled("CMO"), "date": filled("2026-01-01"),
            "signature": {"kind": "text", "value": filled("Dr X")}}]
        study = resources(run(protocol).bundle, "ResearchStudy")[0]
        approval = next(item for item in study["extension"] if item["url"].endswith("m11-approval"))
        urls = [item["url"] for item in approval["extension"]]
        self.assertNotIn("signatureMethod", urls)
        self.assertEqual(sub_extension(approval, "approvalDate")["_valueDate"]["extension"][0]["url"], DATA_ABSENT)


class NotApplicableProvenanceTest(unittest.TestCase):
    def test_model_decided_not_applicable_section_gets_provenance(self):
        protocol = base_protocol()
        node = section(protocol, "2.1")
        node["status"] = "not_applicable"
        node["custody"] = custody("ai_generated", LLM, needs_review=True)
        bundle = run(protocol).bundle
        provenance = resources(bundle, "Provenance")
        self.assertEqual([item["target"][0]["extension"][0]["valueUri"] for item in provenance], ["sec-2.1"])
        self.assertIn("marked not applicable", provenance[0]["text"]["div"])

    def test_script_decided_not_applicable_section_gets_none(self):
        protocol = base_protocol()
        section(protocol, "2.1")["status"] = "not_applicable"
        self.assertEqual(resources(run(protocol).bundle, "Provenance"), [])


class SidecarPathTest(unittest.TestCase):
    def test_generated_json_finds_the_shared_sidecar(self):
        layout, assets = sidecar_paths(Path("out/x.generated.json"))
        self.assertEqual((layout.name, assets.name), ("x.layout.json", "x.assets"))
        self.assertEqual(sidecar_paths(Path("out/x.json"))[0].name, "x.layout.json")


def assert_fhir_xhtml(test, div):
    root = ET.fromstring(div)
    test.assertEqual(root.tag, f"{{{XHTML_NS}}}div")
    test.assertTrue("".join(root.itertext()).strip())
    for node in root.iter():
        test.assertIn(node.tag.removeprefix(f"{{{XHTML_NS}}}"), NARRATIVE_TAGS)
        test.assertFalse([name for name in node.attrib if name.startswith("data-")], node.attrib)


if __name__ == "__main__":
    unittest.main()
