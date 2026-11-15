"""Export protocol JSON to a FHIR document Bundle for the HL7 Clinical Study Protocol IG.

Target: hl7.fhir.uv.clinical-study-protocol 1.0.0-ballot2 on FHIR 6.0.0-ballot3 (package in
resources/reference/hl7-fhir/package.tgz). Paths follow the IG's own mapping sheet,
resources/M11 to FHIR Mapping 02.xlsx: the title page and amendment details become ResearchStudy
elements and extensions, and every section becomes a section of one Composition, coded from the IG
section value set. A section holding AI-drafted content gets a Provenance that names the model.

The protocol JSON is taken as fact: its validity is checked upstream, so every value it holds is
exported as is, whatever its review flags say. The one exception is an item (slot or content block)
whose custody confidence is "low" or below: its value is withheld, a visible marker takes its place, and a
Task asks a person to enter it; the document is then preliminary. A value the profile requires but
the JSON lacks carries the data-absent-reason extension instead of a guess. For a required code that
is not a valid encoding (the binding needs a code), so the report marks it NOT CONFORMANT.
"""

import argparse
import base64
import copy
import json
import mimetypes
import re
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from converter.model import JsonObject, now_iso
from converter.paths import RESOURCES_DIR
from converter.protocol_fields import (
    AI_ORIGINS, LEAD_INS, LOW_CONFIDENCE, MISSING_TEXT, NO_BASIS_CONFIDENCE, NOT_APPLICABLE_TEXT, VERY_LOW, is_ai, is_lead_in,
)
from converter.render.renumber import natural_key


IG_PACKAGE = RESOURCES_DIR / "reference" / "hl7-fhir" / "package.tgz"
IDENTIFIER_SYSTEMS = RESOURCES_DIR / "fhir" / "identifier-systems.json"

IG_STRUCTURES = "http://hl7.org/fhir/uv/clinical-study-protocol/StructureDefinition/"
RESEARCH_STUDY_PROFILE = IG_STRUCTURES + "m11-research-study-profile"
NARRATIVES_PROFILE = IG_STRUCTURES + "m11-research-study-narratives"
EXT_M11_STUDY = IG_STRUCTURES + "m11-research-study"
EXT_NARRATIVE = IG_STRUCTURES + "narrative-elements"
EXT_CONFIDENTIALITY = IG_STRUCTURES + "m11-confidentiality-statement"
EXT_APPROVAL = IG_STRUCTURES + "m11-approval"
EXT_AMENDMENT = IG_STRUCTURES + "m11-protocol-amendment"
EXT_SCOPE_IMPACT = IG_STRUCTURES + "m11-amendment-scope-impact"
EXT_AMENDMENT_DETAIL = IG_STRUCTURES + "m11-amendment-detail"
SECTION_VALUE_SET = "udp-section-codes-vs"
NCIT_CODE_SYSTEM_FILE = "package/CodeSystem-ncit-cs.json"

NCIT = "http://ncicb.nci.nih.gov/xml/owl/EVS/Thesaurus.owl"
DATA_ABSENT = "http://hl7.org/fhir/StructureDefinition/data-absent-reason"
TARGET_ELEMENT = "http://hl7.org/fhir/StructureDefinition/targetElement"
LIST_EMPTY_REASON = "http://terminology.hl7.org/CodeSystem/list-empty-reason"
DATA_OPERATION = "http://terminology.hl7.org/CodeSystem/v3-DataOperation"
PARTICIPANT_TYPE = "http://terminology.hl7.org/CodeSystem/provenance-participant-type"
# FHIR 6.0.0-ballot3 defines these two itself; later R6 builds move them to terminology.hl7.org
DEVICE_NAME_TYPE = "http://hl7.org/fhir/device-nametype"
TASK_CODE = "http://hl7.org/fhir/CodeSystem/task-code"
UCUM = "http://unitsofmeasure.org"
URI_IDENTIFIER = "urn:ietf:rfc:3986"
XHTML_NS = "http://www.w3.org/1999/xhtml"
EXTERNAL_SYSTEMS = {
    "ISO 3166-1": "urn:iso:std:iso:3166",
    "ISO 3166-2": "urn:iso:std:iso:3166:-2",
    "MedDRA": "http://terminology.hl7.org/CodeSystem/mdr",
    "SNOMED CT": "http://snomed.info/sct",
    "UCUM": UCUM,
}

NARRATIVE_CONTENT_TEXT = "C207508"
ACRONYM_TITLE = "C207646"
BRIEF_TITLE = "C207615"
NOT_AMENDED = "C218485"
NOT_APPLICABLE_CODE = "C48660"
OTHER_REASON = "C17649"
YES = "C49488"
NO = "C49487"
PARTY_ROLES = {"sponsor": "C70793", "co_sponsor": "C215669", "local_sponsor": "C215670"}
DEVICE_MANUFACTURER = "C156625"
MEDICAL_EXPERT = "C51876"
POSTAL_ADDRESS = "C70946"
PRODUCT_CODE_TYPE = "C218675"
PRODUCT_NAME_TYPES = {"nonproprietary": "C97054", "proprietary": "C71898"}
ADDRESS_PURPOSES = {"sponsor": "C218677", "co_sponsor": "C218679"}
IDENTIFIER_TYPES = {
    "sponsor_protocol": "C132351",
    "amendment": "C218477",
    "eu_ct_number": "C218684",
    "fda_ind_number": "C218685",
    "ide_number": "C218686",
    "jrct_number": "C218687",
    "nct_number": "C172240",
    "nmpa_ind_number": "C218688",
    "who_utn_number": "C218689",
    "pediatric_investigational_plan_number": "C218690",
    "other": "C218690",
}

BLOCK_TYPES = ("paragraph", "table", "heading")
SECTION_DISPLAY = re.compile(r"^ICH M11 Protocol Section (\d+(?:\.\d+)*) ")
# M11 repeats these sections once per objective; the value set codes the section once, numbered 1
REPEATED_OBJECTIVE = re.compile(r"^(3\.[123]|10\.[45])\.\d+(?=\.|$)")
FIGURE_TEXT = "Figure"
INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
ELEMENT_ID_CHARS = re.compile(r"[^A-Za-z0-9.-]+")
MAX_ELEMENT_ID = 64
LOW_CONFIDENCE_LEVELS = (LOW_CONFIDENCE, VERY_LOW, NO_BASIS_CONFIDENCE)
WITHHELD = "withheld"
NEEDS_INPUT_TEXT = "[Needs human input: withheld from this export because its confidence was low]"
NEEDS_INPUT_CLASS = "needs-human-input"
TASK_CODE_TEXT = "Enter content withheld for low confidence"
UNPLACED_TITLE = "Source content not placed in an M11 section"
EMPTY_HEADING_TEXT = "No text under this heading."

# Attributes FHIR narrative XHTML allows (FHIR fhir-xhtml.xsd); everything else, such as the layout
# sidecar's data-* attributes, is dropped
COMMON_ATTRIBUTES = {"class", "style", "id", "title", "lang", "dir"}
TAG_ATTRIBUTES = {
    "td": {"rowspan", "colspan", "headers", "abbr", "scope", "align", "valign"},
    "th": {"rowspan", "colspan", "headers", "abbr", "scope", "align", "valign"},
    "col": {"span", "width"},
    "colgroup": {"span", "width"},
    "img": {"src", "alt", "width", "height"},
    "a": {"href", "name"},
    "table": {"summary", "width", "border", "frame", "rules", "cellspacing", "cellpadding"},
}
DROPPED_CLASSES = {"page-mark"}


@dataclass(frozen=True)
class IgTerms:
    displays: dict[str, str]
    section_codes: dict[str, str]
    section_value_set: frozenset[str]


def code_system_displays(concepts: list[JsonObject]) -> dict[str, str]:
    displays = {}
    for concept in concepts:
        if "display" in concept:
            displays[concept["code"]] = concept["display"]
        displays.update(code_system_displays(concept.get("concept", [])))
    return displays


def load_ig_terms(package: Path = IG_PACKAGE) -> IgTerms:
    """Code displays, and section number -> code from the section value set.

    Displays come from the IG's NCIt code system, which a validator checks them against. A value set
    display can differ from it, so value sets only fill in codes the code system lacks.
    """
    section_codes: dict[str, str] = {}
    section_value_set: set[str] = set()
    with tarfile.open(package) as tar:
        displays = code_system_displays(json.load(tar.extractfile(NCIT_CODE_SYSTEM_FILE))["concept"])
        for member in tar.getmembers():
            if not re.fullmatch(r"package/ValueSet-[^/]+\.json", member.name):
                continue
            value_set = json.load(tar.extractfile(member))
            is_sections = value_set["url"].endswith(SECTION_VALUE_SET)
            for include in value_set.get("compose", {}).get("include", []):
                for concept in include.get("concept", []):
                    displays.setdefault(concept["code"], concept["display"])
                    if not is_sections:
                        continue
                    section_value_set.add(concept["code"])
                    if match := SECTION_DISPLAY.match(concept["display"]):
                        section_codes[match.group(1)] = concept["code"]
    return IgTerms(displays, section_codes, frozenset(section_value_set))


def load_identifier_systems(path: Path = IDENTIFIER_SYSTEMS) -> dict[str, str]:
    systems = json.loads(path.read_text(encoding="utf-8"))
    return {key: value for key, value in systems.items() if not key.startswith("_") and value}


def filled(slot: JsonObject | None) -> object | None:
    """A slot's value, or None when the slot holds none (missing, not applicable)."""
    return slot.get("value") if slot else None


def absent(reason: str) -> JsonObject:
    return {"extension": [{"url": DATA_ABSENT, "valueCode": reason}]}


def absent_reason(slot: JsonObject | None) -> str:
    if slot and slot.get(WITHHELD):
        return "temp-unknown"
    return "not-applicable" if slot and slot.get("status") == "not_applicable" else "unknown"


def is_slot(value: object) -> bool:
    return isinstance(value, dict) and "status" in value and "value" in value and "custody" in value


def is_block(value: object) -> bool:
    return isinstance(value, dict) and value.get("type") in BLOCK_TYPES and "custody" in value


def clean_text(text: str) -> str:
    return INVALID_XML_CHARS.sub("", text)


def element(tag: str, text: str = "", **attributes: str) -> ET.Element:
    node = ET.Element(tag, {key.rstrip("_"): value for key, value in attributes.items()})
    node.text = clean_text(text) if text else None
    return node


def paragraph(text: str, cls: str = "") -> ET.Element:
    return element("p", text, **({"class_": cls} if cls else {}))


def narrative(elements: list[ET.Element], status: str = "additional") -> JsonObject:
    div = ET.Element("div", {"xmlns": XHTML_NS})
    div.extend(elements)
    if not "".join(div.itertext()).strip():
        # Narrative must hold some text (txt-1); an image alone does not count
        image = div.find(".//img")
        div.append(paragraph((image.get("alt") if image is not None else None) or FIGURE_TEXT))
    return {"status": status, "div": ET.tostring(div, encoding="unicode")}


def humanize(key: str) -> str:
    return key.replace("_", " ").capitalize()


def value_text(value: object) -> str:
    """Readable text for a filled slot value of any slot type."""
    if isinstance(value, dict):
        if "decode" in value and "system" in value:
            return f"{value['decode']} ({value['system']} {value['code']})"
        if "decode" in value:
            return str(value["decode"])
        if "quantity" in value:
            return f"{value['quantity']} {value_text(value['unit'])}"
        return str(value.get("date") or value.get("text") or json.dumps(value, ensure_ascii=False))
    return str(value)


def element_id(text: str) -> str:
    return ELEMENT_ID_CHARS.sub("-", text).strip("-")[:MAX_ELEMENT_ID] or "section"


def all_custodies(value: object, skip: frozenset[str] = frozenset()) -> list[JsonObject]:
    """Every custody record under a node, not descending into keys in `skip`."""
    found = []
    if isinstance(value, dict):
        if isinstance(value.get("custody"), dict):
            found.append(value["custody"])
        for key, item in value.items():
            if key not in skip and key != "custody":
                found.extend(all_custodies(item, skip))
    elif isinstance(value, list):
        for item in value:
            found.extend(all_custodies(item, skip))
    return found


CHILD_KEYS = frozenset({"subsections", "additional_subsections", "objectives", "objective_analyses"})


def is_low_confidence(item: JsonObject) -> bool:
    return item["custody"].get("confidence") in LOW_CONFIDENCE_LEVELS


def withhold_low_confidence(value: object) -> object:
    """A copy with every low-confidence slot emptied and every low-confidence block replaced by a marker."""
    if isinstance(value, list):
        return [withheld_block(item) if is_block(item) and is_low_confidence(item) else withhold_low_confidence(item)
                for item in value]
    if not isinstance(value, dict):
        return value
    if is_slot(value) and is_low_confidence(value) and value["value"] is not None:
        # The withheld value is gone, so it no longer counts as AI content needing a Provenance
        custody = {**value["custody"], "origin": "none", "v10_sources": []}
        return {**copy.deepcopy(value), "value": None, "custody": custody, WITHHELD: True}
    return {key: item if key == "custody" else withhold_low_confidence(item) for key, item in value.items()}


def withheld_block(block: JsonObject) -> JsonObject:
    marker_custody = {**block["custody"], "origin": "none", "v10_sources": []}
    return {"type": "paragraph", "text": NEEDS_INPUT_TEXT, "style": "Normal", "custody": marker_custody, WITHHELD: True}


def withheld_labels(value: object, path: tuple[str, ...] = (), skip: frozenset[str] = frozenset()) -> list[str]:
    """Readable names of the withheld items under a node, not descending into keys in `skip`."""
    if isinstance(value, list):
        if any(is_block(item) and item.get(WITHHELD) for item in value):
            return ["section text" if path[-1:] in ((), ("content",)) else " ".join(path)]
        return [label for index, item in enumerate(value, 1)
                for label in withheld_labels(item, (*path, str(index)), skip)]
    if not isinstance(value, dict):
        return []
    if value.get(WITHHELD):
        return [" ".join(path).capitalize()]
    return [label for key, item in value.items() if key not in skip and key != "custody"
            for label in withheld_labels(item, (*path, humanize(key).lower()), skip)]


@dataclass
class AiSection:
    element_id: str
    title: str
    custodies: list[JsonObject]
    not_applicable: bool = False


@dataclass
class FhirExport:
    bundle: JsonObject
    notes: list[str] = field(default_factory=list)


class FhirExporter:
    def __init__(self, protocol: JsonObject, layout: dict[str, str], assets_dir: Path,
                 terms: IgTerms, systems: dict[str, str], timestamp: str) -> None:
        protocol = withhold_low_confidence(protocol)
        self.protocol = protocol
        self.title_page = protocol["title_page"]
        self.amendment = protocol["amendment_details"]
        self.layout = layout
        self.assets_dir = assets_dir.resolve()
        self.terms = terms
        self.systems = systems
        self.timestamp = timestamp
        self.namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"urn:protocol:{protocol['source']['sha256']}:{timestamp}")
        self.entries: list[JsonObject] = []
        self.notes: list[str] = []
        self.author: str | None = None
        self.ai_sections: list[AiSection] = []
        self.element_ids: set[str] = set()
        self.devices: dict[str, str] = {}
        self.tasks: list[JsonObject] = []

    # Bundle

    def export(self) -> FhirExport:
        composition_url = self.reserve()
        study_url = self.reserve()
        self.entries[1]["resource"] = self.research_study(composition_url)
        for part, node in (("Title page", self.title_page), ("Amendment details", self.amendment)):
            if labels := withheld_labels(node):
                self.request_input(study_url, None, part, labels)
        sections = self.sections()
        if self.tasks:
            self.entries[1]["resource"]["status"] = "draft"
        if self.author is None:
            self.notes.append("FHIR: no sponsor name; Composition.author is an Organization with an absent name")
            self.author = self.add({"resourceType": "Organization", "_name": absent("unknown")})
        self.entries[0]["resource"] = self.composition(study_url, sections)
        for ai_section in self.ai_sections:
            self.add(self.provenance(composition_url, ai_section))
        for task in self.tasks:
            self.add(task)
        if self.tasks:
            self.notes.append(f"FHIR: {len(self.tasks)} Tasks ask for human input on low-confidence items withheld from the export")
        bundle = {
            "resourceType": "Bundle",
            "identifier": {"system": URI_IDENTIFIER, "value": f"urn:uuid:{uuid.uuid5(self.namespace, 'bundle')}"},
            "type": "document",
            "timestamp": self.timestamp,
            "entry": self.entries,
        }
        self.notes.append(f"FHIR: {len(self.entries)} resources, {len(self.ai_sections)} AI-drafted sections with Provenance")
        return FhirExport(bundle, self.notes)

    def reserve(self) -> str:
        url = f"urn:uuid:{uuid.uuid5(self.namespace, str(len(self.entries)))}"
        self.entries.append({"fullUrl": url, "resource": None})
        return url

    def add(self, resource: JsonObject) -> str:
        url = self.reserve()
        self.entries[-1]["resource"] = resource
        return url

    def generated_at(self) -> str:
        steps = [step for step in self.protocol.get("processing_log", []) if step["actor"]["kind"] == "llm"]
        return steps[-1]["at"] if steps else self.timestamp

    # Codes

    def concept(self, code: str, fallback: str | None = None, system: str = NCIT) -> JsonObject:
        display = self.terms.displays.get(code, fallback) if system == NCIT else fallback
        coding = {"system": system, "code": code}
        if display:
            coding["display"] = display
        return {"coding": [coding]}

    def coded(self, value: JsonObject) -> JsonObject:
        """CodeableConcept for a schema CodedValue or ExternalCode."""
        if "system" not in value:
            return self.concept(value["code"], value.get("decode"))
        system = EXTERNAL_SYSTEMS.get(value["system"])
        if system is None:
            return {"text": f"{value['decode']} ({value['system']} {value['code']})"}
        return self.concept(value["code"], value.get("decode"), system)

    def coded_slot(self, slot: JsonObject | None) -> JsonObject | None:
        value = filled(slot)
        return self.coded(value) if value else None

    # ResearchStudy

    def research_study(self, composition_url: str) -> JsonObject:
        page = self.title_page
        study: JsonObject = {
            "resourceType": "ResearchStudy",
            "meta": {"profile": [RESEARCH_STUDY_PROFILE]},
            "extension": self.study_extensions(composition_url),
        }
        if version := filled(page["version_number"]):
            study["version"] = version
        study["identifier"] = self.identifiers()
        if title := filled(page["full_title"]):
            study["title"] = title
        labels = [
            {"type": self.concept(code), "value": text}
            for code, slot in ((ACRONYM_TITLE, page["trial_acronym"]), (BRIEF_TITLE, page["short_title"]))
            if (text := filled(slot))
        ]
        if labels:
            study["label"] = labels
        study["status"] = "active"
        if phase := self.coded_slot(page["trial_phase"]):
            study["phase"] = phase
        if product := self.product():
            study["focus"] = [{"reference": {"reference": product}}]
        if parties := self.parties():
            study["associatedParty"] = parties
        return study

    def study_extensions(self, composition_url: str) -> list[JsonObject]:
        extensions = [
            {"url": EXT_M11_STUDY, "extension": [self.original_protocol(), self.version_date()]},
            {"url": EXT_NARRATIVE, "valueReference": {"reference": composition_url}},
        ]
        if statement := filled(self.title_page["sponsor_confidentiality_statement"]):
            extensions.append({"url": EXT_CONFIDENTIALITY, "valueString": statement})
        if approval := self.approval():
            extensions.append({"url": EXT_APPROVAL, "extension": approval})
        if amendment := self.amendment_extension():
            extensions.append({"url": EXT_AMENDMENT, "extension": amendment})
        return extensions

    def original_protocol(self) -> JsonObject:
        """M11 maps both Original Protocol Indicator and Amendment Details here, with statement codes."""
        if statement := self.coded_slot(self.amendment["statement"]):
            return {"url": "originalProtocol", "valueCodeableConcept": statement}
        indicator = filled(self.title_page["original_protocol"])
        if indicator and indicator["code"] == YES:
            return {"url": "originalProtocol", "valueCodeableConcept": self.concept(NOT_AMENDED)}
        self.not_conformant("originalProtocol", "no amendment statement or original-protocol indicator")
        return {"url": "originalProtocol", "valueCodeableConcept": absent(absent_reason(self.title_page["original_protocol"]))}

    def not_conformant(self, element_name: str, why: str) -> None:
        """A required code the JSON lacks: data-absent-reason marks the gap but cannot satisfy the binding."""
        self.notes.append(
            f"FHIR: NOT CONFORMANT: {element_name} has no code ({why}); the profile requires one, "
            "so the bundle fails validation until it is entered")

    def version_date(self) -> JsonObject:
        slot = self.title_page["version_date"]
        if date := filled(slot):
            return {"url": "versionDate", "valueDate": date}
        self.notes.append(f"FHIR: versionDate absent (version date is {slot['status']})")
        return {"url": "versionDate", "_valueDate": absent(absent_reason(slot))}

    def identifier(self, kind: str, value: str | None, label: str | None = None) -> JsonObject:
        type_concept = self.concept(IDENTIFIER_TYPES[kind])
        if label:
            type_concept["text"] = label
        identifier: JsonObject = {"type": type_concept}
        if system := self.systems.get(kind):
            identifier["system"] = system
        if value is None:
            identifier["_value"] = absent("unknown")
        else:
            identifier["value"] = value
        return identifier

    def identifiers(self) -> list[JsonObject]:
        page = self.title_page
        identifiers = []
        if value := filled(page["sponsor_protocol_identifier"]):
            identifiers.append(self.identifier("sponsor_protocol", value))
        if value := filled(page["amendment_identifier"]):
            identifiers.append(self.identifier("amendment", value))
        for item in page["regulatory_identifiers"]:
            if value := filled(item["id"]):
                identifiers.append(self.identifier(item["type"], value, item.get("label")))
        if not identifiers:
            self.notes.append("FHIR: no identifiers; exported a sponsor protocol identifier with an absent value")
            identifiers.append(self.identifier("sponsor_protocol", None))
        return identifiers

    def product(self) -> str | None:
        page = self.title_page
        codes = [code for slot in page["investigational_product_codes"] if (code := filled(slot))]
        names = [(item["kind"], name) for item in page["investigational_product_names"] if (name := filled(item["name"]))]
        if not codes and not names:
            return None
        system = self.systems.get("product_code")
        product: JsonObject = {"resourceType": "MedicinalProductDefinition"}
        if codes:
            product["identifier"] = [
                {"type": self.concept(PRODUCT_CODE_TYPE), **({"system": system} if system else {}), "value": code}
                for code in codes
            ]
        product["name"] = (
            [{"productName": name, "type": self.concept(PRODUCT_NAME_TYPES[kind])} for kind, name in names]
            or [{"_productName": absent("unknown")}]
        )
        return self.add(product)

    def organization(self, name: str | None, address: str | None, purpose: str) -> str:
        organization: JsonObject = {"resourceType": "Organization"}
        if name:
            organization["name"] = name
        else:
            organization["_name"] = absent("unknown")
        if address:
            organization["contact"] = [{"purpose": self.concept(purpose), "address": {"text": address}}]
        return self.add(organization)

    def parties(self) -> list[JsonObject]:
        page = self.title_page
        parties = []
        for sponsor in page["sponsors"]:
            name, address = filled(sponsor["name"]), filled(sponsor["legal_address"])
            if not name and not address:
                continue
            url = self.organization(name, address, ADDRESS_PURPOSES.get(sponsor["role"], POSTAL_ADDRESS))
            parties.append({"role": self.concept(PARTY_ROLES[sponsor["role"]]), "party": {"reference": url}})
            if sponsor["role"] == "sponsor" and self.author is None and name:
                self.author = url
        for manufacturer in page["device_manufacturers"]:
            name, address = filled(manufacturer["name"]), filled(manufacturer["legal_address"])
            if name or address:
                url = self.organization(name, address, POSTAL_ADDRESS)
                parties.append({"role": self.concept(DEVICE_MANUFACTURER), "party": {"reference": url}})
        if contact := filled(page["medical_expert_contact"]):
            # The mapping puts this free text in Practitioner.text.div: it may be a location, not a name
            url = self.add({"resourceType": "Practitioner", "text": narrative([paragraph(contact)])})
            parties.append({"role": self.concept(MEDICAL_EXPERT), "party": {"reference": url}})
        return parties

    def approval(self) -> list[JsonObject]:
        parts = []
        approval = filled(self.title_page["sponsor_approval"])
        if approval and "date" in approval:
            parts.append({"url": "approvalDate", "valueDate": approval["date"]})
        location = approval.get("text") if approval else None
        signatories = self.title_page["sponsor_signatories"]
        signatory = signatories[0] if signatories else None
        if len(signatories) > 1:
            self.notes.append(f"FHIR: m11-approval holds one signature; {len(signatories) - 1} more signatories not exported")
        if signatory:
            if name := filled(signatory["name"]):
                practitioner: JsonObject = {"resourceType": "Practitioner", "name": [{"text": name}]}
                if title := filled(signatory["title"]):
                    practitioner["qualification"] = [{"code": {"text": title}}]
                parts.append({"url": "signature", "valueSignature": {"who": {"reference": self.add(practitioner)}}})
            signature = signatory["signature"]
            if (text := filled(signature["value"])) and signature["kind"] == "location":
                location = location or text
            elif text and signature["kind"] == "text":
                # signatureMethod describes how the protocol is signed; a typed signature is not that
                self.notes.append("FHIR: typed signature text not exported (m11-approval has no element for it)")
        if location:
            parts.append({"url": "signatureUrl", "valueString": location})
        elif parts and not (approval and "date" in approval):
            # The extension's date-required constraint wants an approval date or a location
            self.notes.append("FHIR: approvalDate absent (no sponsor approval date or signature location)")
            parts.insert(0, {"url": "approvalDate", "_valueDate": absent(absent_reason(self.title_page["sponsor_approval"]))})
        return parts

    # Amendment

    def is_amendment(self) -> bool:
        indicator = filled(self.title_page["original_protocol"])
        if indicator and indicator["code"] == YES:
            return False
        details = {key: value for key, value in self.amendment.items() if key != "statement"}
        has_details = any(custody.get("origin") not in (None, "none") for custody in all_custodies(details))
        return bool(indicator) or bool(filled(self.title_page["amendment_identifier"])) or has_details

    def amendment_extension(self) -> list[JsonObject]:
        if not self.is_amendment():
            return []
        page, details = self.title_page, self.amendment
        identifier = filled(page["amendment_identifier"])
        parts = [{"url": "identifier", "valueIdentifier": (
            self.identifier("amendment", identifier) if identifier else absent("unknown"))}]
        parts.append({"url": "scope", "valueCodeableConcept": self.amendment_scope()})
        for item in page["amendment_scope_identifiers"]:
            if item["kind"] in ("country", "region") and (code := filled(item.get("country_or_region"))):
                parts.append({"url": item["kind"], "valueCodeableConcept": self.coded(code)})
            elif item["kind"] == "site" and (site := filled(item.get("site"))):
                parts.append({"url": "site", "valueIdentifier": {"value": site}})
        if impact := self.scope_impact():
            parts.append({"url": EXT_SCOPE_IMPACT, "extension": impact})
        primary = [self.reason(code, details["primary_reason_other"]) for slot in details["primary_reasons"] if (code := filled(slot))]
        if primary:
            parts.append({"url": "primaryReason", "valueCodeableConcept": primary[0]})
        if len(primary) > 1:
            self.notes.append(f"FHIR: primaryReason holds one reason; {len(primary) - 1} more primary reasons not exported")
        for slot in details["secondary_reasons"]:
            if code := filled(slot):
                parts.append({"url": "secondaryReason", "valueCodeableConcept": self.reason(code, details["secondary_reason_other"])})
        text_parts = (
            ("summary", "summary"),
            ("substantialImpactSafetyComment", "safety_or_rights_explanation"),
            ("substantialImpactReliabilityComment", "data_reliability_explanation"),
        )
        for url, key in text_parts:
            if text := filled(details[key]):
                parts.append({"url": url, "valueString": text})
        coded_parts = (
            ("substantialImpactSafety", "substantial_impact_on_safety_or_rights"),
            ("substantialImpactReliability", "substantial_impact_on_data_reliability"),
        )
        for url, key in coded_parts:
            if concept := self.coded_slot(details[key]):
                parts.append({"url": url, "valueCodeableConcept": concept})
        parts.extend({"url": EXT_AMENDMENT_DETAIL, "extension": self.change(change)} for change in details["changes"])
        return parts

    def amendment_scope(self) -> JsonObject:
        slot = self.title_page["amendment_scope"]
        if concept := self.coded_slot(slot):
            return concept
        # The mapping sheet: M11's "leave blank if not applicable" becomes an explicit Not Applicable
        if slot["status"] == "not_applicable":
            return self.concept(NOT_APPLICABLE_CODE)
        self.notes.append("FHIR: amendment scope absent")
        return absent("unknown")

    def scope_impact(self) -> list[JsonObject]:
        enrolled = self.amendment["approximate_enrolled"]
        quantity, unit = filled(enrolled["quantity"]), filled(enrolled["unit"])
        scope = self.coded_slot(self.amendment["enrollment_scope"])
        if quantity is None and scope is None:
            return []
        if quantity is None:
            number = {"url": "number", "_valuePositiveInt": absent("unknown")}
        elif unit == "percent":
            number = {"url": "number", "valueQuantity": {"value": quantity, "unit": "%", "system": UCUM, "code": "%"}}
        elif quantity >= 1 and float(quantity).is_integer():
            number = {"url": "number", "valuePositiveInt": int(quantity)}
        else:
            number = {"url": "number", "valueQuantity": {"value": quantity}}
        if scope is None:
            self.not_conformant("amendment scope impact scope", "no enrollment scope")
        return [number, {"url": "scope", "valueCodeableConcept": scope or absent("unknown")}]

    def reason(self, code: JsonObject, other: JsonObject) -> JsonObject:
        concept = self.coded(code)
        if code["code"] == OTHER_REASON and (text := filled(other)):
            concept["text"] = text
        return concept

    def change(self, change: JsonObject) -> list[JsonObject]:
        description, rationale = filled(change["description"]), filled(change["rationale"])
        section_code = filled(change["section"])
        section = self.coded(section_code) if section_code else None
        if section is None:
            self.not_conformant("amendment change section", "no section named for the change")
        elif section_code["code"] not in self.terms.section_value_set:
            self.notes.append(
                f"FHIR: NOT CONFORMANT: amendment change section {section_code['code']} is not in the IG "
                "section value set, which the profile requires")
        return [
            {"url": "detail", "valueString": description} if description else {"url": "detail", "_valueString": absent("unknown")},
            {"url": "rationale", "valueString": rationale} if rationale else {"url": "rationale", "_valueString": absent("unknown")},
            {"url": "section", "valueCodeableConcept": section or absent("unknown")},
        ]

    # Composition

    def composition(self, study_url: str, sections: list[JsonObject]) -> JsonObject:
        title = filled(self.title_page["full_title"]) or self.protocol["source"]["file_name"]
        # Withheld items still need a person to enter them
        status = "preliminary" if self.tasks else "final"
        return {
            "resourceType": "Composition",
            "meta": {"profile": [NARRATIVES_PROFILE]},
            "text": narrative([paragraph(title), paragraph(f"Status: {status}")], "generated"),
            "status": status,
            "type": self.concept(NARRATIVE_CONTENT_TEXT),
            "subject": [{"reference": study_url}],
            "date": self.generated_at(),
            "author": [{"reference": self.author}],
            "title": title,
            "section": sections,
        }

    def sections(self) -> list[JsonObject]:
        top = self.protocol["sections"]
        sections = [entry for key in sorted(top, key=int) if (entry := self.section(top[key], key))]
        if unplaced := self.unplaced():
            sections.append(unplaced)
        return sections

    def unique_id(self, text: str) -> str:
        base = candidate = element_id(text)
        counter = 1
        while candidate in self.element_ids:
            counter += 1
            suffix = f"-{counter}"
            candidate = base[:MAX_ELEMENT_ID - len(suffix)] + suffix
        self.element_ids.add(candidate)
        return candidate

    def section(self, node: JsonObject, key: str) -> JsonObject | None:
        status = node.get("status", "populated")
        if status == "omitted":
            return None
        number = node.get("number")
        title = f"{number} {node['title']}" if number else str(node["title"])
        entry: JsonObject = {"id": self.unique_id(f"sec-{key}"), "title": title}
        if number and (code := self.terms.section_codes.get(REPEATED_OBJECTIVE.sub(r"\g<1>.1", number))):
            entry["code"] = self.concept(code)
        body = self.body(node, number, status)
        children = self.children(node, key)
        if body:
            entry["text"] = narrative(body)
        elif not children:
            entry["text"] = narrative([paragraph(EMPTY_HEADING_TEXT)])
        if status == "missing" and not children:
            entry["emptyReason"] = {"coding": [{"system": LIST_EMPTY_REASON, "code": "unavailable"}]}
        if children:
            entry["section"] = children
        ai = [custody for custody in all_custodies({k: v for k, v in node.items() if k != "custody"}, CHILD_KEYS)
              if custody.get("origin") in AI_ORIGINS]
        # A model's not-applicable decision is AI content even though the section holds no text
        decided_by_model = status == "not_applicable" and node["custody"].get("origin") in AI_ORIGINS
        if decided_by_model:
            ai.append(node["custody"])
        if ai:
            self.ai_sections.append(AiSection(entry["id"], title, ai, decided_by_model))
        if labels := withheld_labels(node, skip=CHILD_KEYS):
            self.request_input(self.entries[0]["fullUrl"], entry["id"], f"Section {title}", labels)
        return entry

    def request_input(self, focus_url: str, element: str | None, where: str, labels: list[str]) -> None:
        """A Task asking a person to enter what was withheld; the focus names the resource and section."""
        focus: JsonObject = {"reference": focus_url}
        if element:
            focus["extension"] = [{"url": TARGET_ELEMENT, "valueUri": element}]
        items = ", ".join(sorted(set(labels)))
        description = f"{where}: enter {items}. The drafted value had low confidence and was left out of this export."
        self.tasks.append({
            "resourceType": "Task",
            "text": narrative([paragraph(description)], "generated"),
            "status": "requested",
            "intent": "order",
            "code": {"coding": [{"system": TASK_CODE, "code": "change"}], "text": TASK_CODE_TEXT},
            "description": description,
            "focus": focus,
            "authoredOn": self.timestamp,
        })

    def children(self, node: JsonObject, key: str) -> list[JsonObject]:
        children = []
        subsections = node.get("subsections", {})
        if isinstance(subsections, dict):
            pairs = [(child_key, subsections[child_key]) for child_key in sorted(subsections, key=natural_key)]
        else:
            pairs = [(f"{key}-{index}", child) for index, child in enumerate(subsections, 1)]
        for objective in node.get("objectives", []):
            pairs.append((objective["id"], objective))
        for analysis in node.get("objective_analyses", []):
            pairs.append((analysis["number"], analysis))
        for index, extra in enumerate(node.get("additional_subsections", []), 1):
            pairs.append((f"{key}-added-{index}", extra))
        for child_key, child in pairs:
            if entry := self.section(child, child_key):
                children.append(entry)
        return children

    def body(self, node: JsonObject, number: str | None, status: str) -> list[ET.Element]:
        elements = []
        if status == "not_applicable":
            elements.append(paragraph(NOT_APPLICABLE_TEXT))
        elif status == "missing":
            elements.append(paragraph(MISSING_TEXT, "missing"))
        content = list(node.get("content", []))
        if status == "populated" and number in LEAD_INS:
            elements.append(paragraph(LEAD_INS[number]))
            if content and is_lead_in(content[0]):
                content = content[1:]
        for name in ("category_label", "statement", "estimands", "additional_attributes"):
            if name in node:
                elements.extend(self.field_elements(node[name], humanize(name)))
        for block in content:
            elements.extend(self.block(block))
        if "fields" in node:
            elements.extend(self.field_elements(node["fields"], ""))
        return elements

    def field_elements(self, value: object, label: str) -> list[ET.Element]:
        """Filled slots as labelled paragraphs, in schema order; blocks inside fields render as blocks."""
        if is_slot(value):
            text = filled(value)
            if value.get(WITHHELD):
                text = NEEDS_INPUT_TEXT
            if text is None:
                return []
            line = paragraph("")
            bold = ET.SubElement(line, "b")
            bold.text = clean_text(f"{label}: ")
            bold.tail = clean_text(value_text(text))
            return [line]
        if is_block(value):
            return self.block(value)
        if isinstance(value, dict):
            if "label" in value and "value" in value:
                return self.field_elements(value["value"], str(value["label"]))
            items = [(key, item) for key, item in value.items() if key not in ("custody", "id", "objective_id")]
            return [node for key, item in items
                    for node in self.field_elements(item, f"{label} {humanize(key).lower()}".strip().capitalize())]
        if isinstance(value, list):
            return [node for index, item in enumerate(value, 1)
                    for node in self.field_elements(item, f"{label} {index}".strip())]
        return []

    def block(self, block: JsonObject) -> list[ET.Element]:
        if block.get(WITHHELD):
            return [paragraph(NEEDS_INPUT_TEXT, NEEDS_INPUT_CLASS)]
        if not is_ai(block) and (xhtml := self.layout.get(str(block.get("source_index")))):
            return self.layout_elements(xhtml)
        cls = "ai-generated" if is_ai(block) else ""
        if block["type"] == "table":
            table = element("table", **({"class_": cls} if cls else {}))
            for row in block["rows"]:
                tr = ET.SubElement(table, "tr")
                for cell in row:
                    ET.SubElement(tr, "td").text = clean_text(cell)
            return [table]
        if block["type"] == "heading":
            line = paragraph("", cls)
            ET.SubElement(line, "b").text = clean_text(block["text"])
            return [line]
        return [paragraph(block["text"], cls)]

    def layout_elements(self, xhtml: str) -> list[ET.Element]:
        root = ET.fromstring(f"<div>{xhtml}</div>")
        self.clean(root)
        return list(root)

    def clean(self, node: ET.Element) -> None:
        for child in list(node):
            if child.get("class") in DROPPED_CLASSES:
                remove_keeping_tail(node, child)
                continue
            allowed = COMMON_ATTRIBUTES | TAG_ATTRIBUTES.get(child.tag, set())
            for name in [name for name in child.attrib if name not in allowed]:
                del child.attrib[name]
            if child.tag == "img":
                child.set("src", self.image_data(child.get("src", "")))
            self.clean(child)

    def image_data(self, src: str) -> str:
        path = (self.assets_dir / Path(src).name).resolve()
        if not path.is_relative_to(self.assets_dir) or not path.is_file():
            raise FileNotFoundError(f"Figure {src!r} is not in {self.assets_dir}")
        media_type = mimetypes.guess_type(path.name)[0] or "image/png"
        return f"data:{media_type};base64," + base64.b64encode(path.read_bytes()).decode("ascii")

    def unplaced(self) -> JsonObject | None:
        items = self.protocol.get("unplaced_v10_content", [])
        if not items:
            return None
        elements = []
        for item in items:
            if heading := item["v10_source"].get("found_heading"):
                line = paragraph("")
                ET.SubElement(line, "b").text = clean_text(heading)
                elements.append(line)
            for block in item["blocks"]:
                elements.extend(self.block(block))
        entry = {"id": self.unique_id("sec-unplaced"), "title": UNPLACED_TITLE,
                 "text": narrative(elements or [paragraph(EMPTY_HEADING_TEXT)])}
        ai = [custody for custody in all_custodies(items) if custody.get("origin") in AI_ORIGINS]
        if ai:
            self.ai_sections.append(AiSection(entry["id"], UNPLACED_TITLE, ai))
        if any(block.get(WITHHELD) for item in items for block in item["blocks"]):
            self.request_input(self.entries[0]["fullUrl"], entry["id"], UNPLACED_TITLE, ["section text"])
        return entry

    # Provenance

    def device(self, actor: JsonObject) -> str:
        name = f"{actor.get('name', 'llm')} ({actor.get('model', 'unknown model')})"
        if name not in self.devices:
            name_type = {"coding": [{"system": DEVICE_NAME_TYPE, "code": "user-friendly-name"}]}
            self.devices[name] = self.add({
                "resourceType": "Device",
                "text": narrative([paragraph(name)], "generated"),
                "name": [{"value": name, "type": name_type}],
            })
        return self.devices[name]

    def provenance(self, composition_url: str, section: AiSection) -> JsonObject:
        generated_only = all(custody["origin"] == "ai_generated" for custody in section.custodies)
        actors = {json.dumps(custody["actor"], sort_keys=True): custody["actor"] for custody in section.custodies}
        models = ", ".join(sorted(actor.get('model', 'unknown model') for actor in actors.values()))
        action = "drafted" if generated_only else "rewritten from source text"
        if section.not_applicable:
            action = "marked not applicable" if generated_only else f"{action} and marked not applicable"
        summary = f"Section {section.title}: {action} by {models}."
        return {
            "resourceType": "Provenance",
            "text": narrative([paragraph(summary)], "generated"),
            "target": [{"reference": composition_url,
                        "extension": [{"url": TARGET_ELEMENT, "valueUri": section.element_id}]}],
            "recorded": self.generated_at(),
            "activity": {"coding": [{"system": DATA_OPERATION, "code": "CREATE" if generated_only else "UPDATE"}]},
            "agent": [{"type": {"coding": [{"system": PARTICIPANT_TYPE, "code": "author"}]},
                       "who": {"reference": self.device(actor)}} for actor in actors.values()],
        }


def remove_keeping_tail(parent: ET.Element, child: ET.Element) -> None:
    if child.tail:
        siblings = list(parent)
        index = siblings.index(child)
        if index:
            previous = siblings[index - 1]
            previous.tail = (previous.tail or "") + child.tail
        else:
            parent.text = (parent.text or "") + child.tail
    parent.remove(child)


def export_fhir(protocol: JsonObject, layout: dict[str, str], assets_dir: Path, timestamp: str | None = None,
                terms: IgTerms | None = None, systems: dict[str, str] | None = None) -> FhirExport:
    return FhirExporter(
        protocol, layout, assets_dir, terms or load_ig_terms(),
        load_identifier_systems() if systems is None else systems, timestamp or now_iso(),
    ).export()


def sidecar_paths(protocol_path: Path) -> tuple[Path, Path]:
    """The layout sidecar and asset folder written next to <name>.json and <name>.generated.json."""
    name = protocol_path.name.removesuffix(".json").removesuffix(".generated")
    return protocol_path.with_name(f"{name}.layout.json"), protocol_path.with_name(f"{name}.assets")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("protocol", type=Path, help="<name>.generated.json (or <name>.json)")
    parser.add_argument("--out", type=Path, help="default: <name>.fhir.json next to the input")
    args = parser.parse_args()
    layout_path, assets_dir = sidecar_paths(args.protocol)
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    layout = json.loads(layout_path.read_text(encoding="utf-8")) if layout_path.exists() else {}
    result = export_fhir(protocol, layout, assets_dir)
    out = args.out or layout_path.with_name(layout_path.name.removesuffix(".layout.json") + ".fhir.json")
    out.write_text(json.dumps(result.bundle, indent=1, ensure_ascii=False), encoding="utf-8")
    print("\n".join([f"wrote {out}", *result.notes]))


if __name__ == "__main__":
    main()
