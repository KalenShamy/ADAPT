"""Fill structured fields (Overall Design, interventions table, safety reporting, country requirements,
amendment history) by asking an LLM to read them from the protocol's own text."""

import copy
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date

from converter.generate.client import LlmClient, LlmError
from converter.generate.context import SourceIndex, merge_sources, section_context
from converter.generate.fill import NO_BASIS, NO_BASIS_CONFIDENCE, now_iso
from converter.generate.guidance import format_lines, section_format
from converter.generate.prompts import CONFIDENCE_LEVELS, parse_response
from converter.title_page import render_values
from pdf_to_cpt_docx import amendment_state
from converter.terminology import TermRegistry, normalize
from converter.terminology.registry import default_registry

type JsonObject = dict[str, object]
type Validator = Callable[[JsonObject], list[str]]

MAX_OFFERED_CODES = 40
LLM_CONFIDENCE = "medium"  # when the model gives no confidence of its own
NOT_APPLICABLE_ANSWER = "not applicable"
ISO_COUNTRY_SYSTEM = "ISO 3166-1"
SCRIPT_ACTOR: JsonObject = {"kind": "script", "name": "converter.generate.fields", "version": "1"}
EVIDENCE_KEY = "evidence"
MIN_EVIDENCE_WORDS = 3  # shorter quotes ("24 hours", "SAE") match almost any safety text, unless they are the value
UNSUPPORTED_NOTE = "No quote from the source supports this value."
NOT_IN_SOURCE_KEY = "not_in_source"
NOT_IN_SOURCE_NOTE = "The source does not state this value; the model filled in the most likely one."
REVIEW_NOTES = (UNSUPPORTED_NOTE, NOT_IN_SOURCE_NOTE)
QUOTE_CHARACTERS = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                                  "–": "-", "—": "-", " ": " "})
EVIDENCE_INSTRUCTION = (
    f'Each row object also has an "{EVIDENCE_KEY}" object mapping each text field you fill to the words in the SOURCE '
    "PROTOCOL that state it, copied exactly as one contiguous phrase. Leave a field out of "
    f'"{EVIDENCE_KEY}" when no source words state it. Each row object also has a "{NOT_IN_SOURCE_KEY}" list naming '
    "every field you filled although the source does not state it for this row (your most likely value); use [] "
    "when the source states every field you filled."
)

INSTRUCTIONS = """\
You are reading structured fields of an ICH M11 clinical trial protocol from the SOURCE PROTOCOL below.
Answer every field. Use the value the source states. When the source does not state it, give the most likely value
for this trial when what the source does say reasonably implies one. When it gives no basis even for a reasonable
inference, answer null; never guess a date, number, dose or name. Answer "not applicable" only when the trial itself
has no such thing (for example blinded roles in an open-label trial, a comparator description for a placebo control,
amendment details for an original protocol), never because the source does not mention it. Silence is not a fact.
Give each answer a confidence: "high" when the source states it directly, "medium" when you combined facts from
several places, "low" when you inferred it from what the source says, "very_low" when it is a reasonable inference
the source only thinly supports.
For a field with one of a list of allowed values, answer with one of them exactly as written.
Whole numbers only for number fields. Dates are YYYY-MM-DD.
Text answers are finished protocol text. Never mention "the source", "the text" or "the protocol provided", and never
write that the source does not describe something.
For a list of rows, give only the rows that apply to this trial; never add a row only to say something does not apply.
"""


# ---------------------------------------------------------------- schema -> field descriptions

def resolve(schema: JsonObject, node: JsonObject) -> JsonObject:
    while "$ref" in node:
        node = schema["$defs"][node["$ref"].rsplit("/", 1)[-1]]
    return node


def code_options(code_schema: JsonObject) -> dict[str, str]:
    """decode -> code for a Code_* schema."""
    return {option["properties"]["decode"]["const"]: option["properties"]["code"]["const"]
            for option in code_schema["oneOf"]}


def describe(schema: JsonObject, node: JsonObject) -> JsonObject:
    """How to ask for and build a slot, a list of slots, or a record of them.

    kinds: code, enum, integer, number, duration, text, date, external (a coded term looked up in a terminology, else kept as source text), list, record,
    section (a code list too long to offer the model: the model gives the section as the source writes it and
    we match its title against the list).
    """
    node = resolve(schema, node)
    if node.get("type") == "array":
        return {"kind": "list", "item": describe(schema, node["items"]), "min_items": node.get("minItems", 0)}
    properties = node.get("properties", {})
    if "value" not in properties:
        return {"kind": "record", "fields": {name: with_lookup(describe(schema, child), name)
                                              for name, child in properties.items() if name != "custody"}}
    value = resolve(schema, properties["value"]["anyOf"][0])
    if "oneOf" in value:
        codes = code_options(value)
        return {"kind": "code" if len(codes) <= MAX_OFFERED_CODES else "section", "codes": codes}
    if "enum" in value:
        return {"kind": "enum", "options": value["enum"]}
    if value.get("type") == "integer":
        return {"kind": "integer"}
    if value.get("type") == "number":
        return {"kind": "number"}
    if "quantity" in value.get("properties", {}):
        return {"kind": "duration", "codes": code_options(resolve(schema, value["properties"]["unit"]))}
    if "system" in value.get("properties", {}):
        return {"kind": "external"}
    if value.get("format") == "date":
        return {"kind": "date"}
    return {"kind": "text"}


def with_lookup(description: JsonObject, name: str) -> JsonObject:
    """Tag external-term slots with their field name, which picks the terminology to look them up in."""
    if description["kind"] == "external":
        description["lookup"] = name
    elif description["kind"] == "list":
        with_lookup(description["item"], name)
    return description


def prompt_line(name: str, description: JsonObject) -> str:
    kind = description["kind"]
    if kind == "code":
        return f"{name}: one of {json.dumps(list(description['codes']))}"
    if kind == "enum":
        return f"{name}: one of {json.dumps(description['options'])}"
    if kind == "integer":
        return f"{name}: whole number"
    if kind == "number":
        return f"{name}: number"
    if kind == "duration":
        return f'{name}: {{"quantity": whole number, "unit": one of {json.dumps(list(description["codes"]))}}}'
    if kind == "date":
        return f"{name}: date YYYY-MM-DD"
    if kind == "external":
        return f"{name}: the term as the source writes it"
    if kind == "section":
        return f"{name}: the number and title of the protocol section, as the source writes it"
    if kind == "list":
        return f"{name}: list of ({prompt_line('item', description['item'])})"
    if kind == "record":
        inner = "; ".join(prompt_line(key, child) for key, child in description["fields"].items())
        return f"{name}: object with {{{inner}}}"
    return f"{name}: text from the source"


# ---------------------------------------------------------------- context

@dataclass
class Context:
    text: str = ""
    sources: list[JsonObject] = field(default_factory=list)
    terms: TermRegistry = field(default_factory=default_registry)
    confidence: str = LLM_CONFIDENCE  # the model's confidence in the answer being built


def with_confidence(context: Context, answer: object) -> Context:
    """The context for building one answer, carrying the confidence the model gave it."""
    confidence = answer.get("confidence") if isinstance(answer, dict) else None
    return replace(context, confidence=confidence if confidence in CONFIDENCE_LEVELS else LLM_CONFIDENCE)


def answer_value(raw: object) -> tuple[object, object]:
    """(value, the answer object holding its confidence) for a field answered as {"value": ..., "confidence": ...};
    a bare value is taken as it is. No field's own value is an object with a "value" key."""
    if isinstance(raw, dict) and "value" in raw:
        return raw["value"], raw
    return raw, None


def build_context(protocol: JsonObject, number: str, seeds: tuple[str, ...] = ()) -> Context:
    """The source text related to section `number` (converter.generate.context); `seeds` are sections known to
    hold its facts."""
    chosen = section_context(SourceIndex.build(protocol), number, seeds=seeds)
    return Context(chosen.text, chosen.sources)


def format_block(*numbers: str) -> str:
    """The ICH M11 format of the sections an extraction fills, for its prompt."""
    blocks = []
    for number in numbers:
        section = section_format(M11_FORMAT_KEYS.get(number, number))
        lines = format_lines(section)
        if lines:
            blocks.append(f"ICH M11 FORMAT FOR {number} {section.title}:\n" + "\n".join(lines))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- building slots

def missing_slot() -> JsonObject:
    custody = {"origin": "none", "v10_sources": [], "actor": dict(SCRIPT_ACTOR), "confidence": "high", "needs_review": False}
    return {"value": None, "status": "missing", "custody": custody}


def no_basis_slot(client: LlmClient) -> JsonObject:
    """A missing slot the model was asked for and found nothing to infer it from; rendered as a note for a person.
    The schema requires origin `none` on a missing slot, so confidence `none` carries the mark."""
    custody = {"origin": "none", "v10_sources": [], "actor": {"kind": "llm", "name": client.name, "model": client.model},
               "confidence": NO_BASIS_CONFIDENCE, "needs_review": True}
    return {"value": None, "status": "missing", "custody": custody}


def mark_no_basis(built: object, client: LlmClient) -> object:
    """`built` (a missing slot, or a list of them) with each missing slot replaced by a no-basis slot."""
    if isinstance(built, list):
        return [mark_no_basis(item, client) for item in built]
    if isinstance(built, dict) and built.get("status") == "missing":
        return no_basis_slot(client)
    return built


def ai_custody(context: Context, client: LlmClient) -> JsonObject:
    return {
        "origin": "ai_transformed", "v10_sources": copy.deepcopy(context.sources),
        "actor": {"kind": "llm", "name": client.name, "model": client.model},
        "confidence": context.confidence, "needs_review": True,
    }


def typed_value(description: JsonObject, raw: object) -> object | None:
    """The slot value for the model's raw answer, or None when it isn't one the schema allows."""
    kind = description["kind"]
    if kind == "code":
        code = description["codes"].get(raw) if isinstance(raw, str) else None
        return {"code": code, "decode": raw} if code else None
    if kind == "enum":
        return raw if raw in description["options"] else None
    if kind == "integer":
        return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else None
    if kind == "number":
        return raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= 0 else None
    if kind == "duration" and isinstance(raw, dict):
        unit = typed_value({"kind": "code", "codes": description["codes"]}, raw.get("unit"))
        quantity = raw.get("quantity")
        if unit and isinstance(quantity, int) and not isinstance(quantity, bool) and quantity >= 0:
            return {"quantity": quantity, "unit": unit}
        return None
    if kind == "date" and isinstance(raw, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            date.fromisoformat(raw)
        except ValueError:
            return None
        return raw
    if kind == "text" and isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


SECTION_NUMBER_PREFIX = re.compile(r"^(?:sections?\s+)?\d+(?:\.\d+)*\.?\s*", re.IGNORECASE)


def section_code(codes: dict[str, str], raw: str) -> JsonObject | None:
    """The code whose section title equals the title in raw. Section numbers differ between template
    versions, so only the title is compared."""
    def title(text: str) -> str:
        return normalize(SECTION_NUMBER_PREFIX.sub("", text.strip()))

    raw_title = title(raw)
    matches = [(decode, code) for decode, code in codes.items() if raw_title and title(decode) == raw_title]
    return {"code": matches[0][1], "decode": matches[0][0]} if len(matches) == 1 else None


def unsure_slot(raw: str, context: Context) -> JsonObject:
    return {"value": None, "status": "unsure", "raw_text": raw.strip(), "custody": {
        "origin": "v10_derived", "v10_sources": copy.deepcopy(context.sources), "actor": dict(SCRIPT_ACTOR),
        "confidence": context.confidence, "needs_review": True}}


def not_applicable_slot(context: Context, client: LlmClient) -> JsonObject:
    return {"value": None, "status": "not_applicable", "custody": ai_custody(context, client)}


def is_not_applicable_answer(raw: object) -> bool:
    """'not applicable', or the same with a reason after it ("not applicable; no devices are used")."""
    return isinstance(raw, str) and raw.strip().lower().startswith(NOT_APPLICABLE_ANSWER)


def build_slot(description: JsonObject, raw: object, context: Context, client: LlmClient) -> JsonObject:
    """A filled slot, an `unsure` slot holding the source term (external codes), a not_applicable slot, or a missing
    slot."""
    if description["kind"] == "external":
        if not (isinstance(raw, str) and raw.strip()):
            return missing_slot()
        term = context.terms.resolve(description.get("lookup", ""), raw.strip())
        if term:
            return {"value": {"system": term.system, "code": term.code, "decode": term.decode},
                    "status": "filled", "raw_text": raw.strip(), "custody": ai_custody(context, client)}
        return unsure_slot(raw, context)
    if description["kind"] == "section":
        if not (isinstance(raw, str) and raw.strip()):
            return missing_slot()
        value = section_code(description["codes"], raw)
        if value is None:
            return unsure_slot(raw, context)
        return {"value": value, "status": "filled", "raw_text": raw.strip(), "custody": ai_custody(context, client)}
    if description["kind"] == "text" and is_not_applicable_answer(raw):
        return not_applicable_slot(context, client)
    # coded lists can hold a real "Not applicable ..." choice, so for them a match on the list comes first
    value = typed_value(description, raw)
    if value is None and isinstance(raw, str) and raw.strip().lower() == NOT_APPLICABLE_ANSWER:
        return not_applicable_slot(context, client)
    if value is None:
        return missing_slot()
    return {"value": value, "status": "filled", "custody": ai_custody(context, client)}


def build_value(description: JsonObject, raw: object, context: Context, client: LlmClient) -> object:
    """A slot, list of slots, or record of them for the model's raw answer."""
    kind = description["kind"]
    if kind == "list":
        items = raw if isinstance(raw, list) else []
        built = [build_value(description["item"], item, context, client) for item in items]
        built = [item for item in built if has_content(item)]
        return built or ([missing_slot()] if description["min_items"] and description["item"]["kind"] != "record" else [])
    if kind == "record":
        answer = raw if isinstance(raw, dict) else {}
        return {name: build_value(child, answer.get(name), context, client) for name, child in description["fields"].items()}
    return build_slot(description, raw, context, client)


def has_content(built: object) -> bool:
    if isinstance(built, dict) and "status" in built:
        return built["status"] != "missing"
    if isinstance(built, dict):
        return any(has_content(value) for value in built.values())
    if isinstance(built, list):
        return any(has_content(item) for item in built)
    return False


def mark_section_ai(section: JsonObject, context: Context, client: LlmClient) -> None:
    """A section holding an LLM-read slot is ai_transformed as a whole (validator rule)."""
    if "custody" not in section:
        return
    custody = section["custody"]
    custody["origin"] = "ai_transformed"
    custody["v10_sources"] = merge_sources([custody.get("v10_sources", []), context.sources])
    custody["actor"] = {"kind": "llm", "name": client.name, "model": client.model}
    custody["needs_review"] = True
    custody.pop("review", None)
    if section["status"] == "missing":
        section["status"] = "populated"


# ---------------------------------------------------------------- one extraction

def ask(prompt: str, client: LlmClient) -> JsonObject | None:
    try:
        return parse_response(client.complete(prompt))
    except (LlmError, ValueError, json.JSONDecodeError):
        return None


AMENDMENT_DETAILS = "amendment_details"  # the one extraction target that is a top-level block, not a section
M11_FORMAT_KEYS = {AMENDMENT_DETAILS: "Amendment Details"}


def section_at(protocol: JsonObject, number: str) -> JsonObject:
    if number == AMENDMENT_DETAILS:
        return protocol[AMENDMENT_DETAILS]
    parts = number.split(".")
    node = protocol["sections"][parts[0]]
    for depth in range(2, len(parts) + 1):
        node = node["subsections"][".".join(parts[:depth])]
    return node


def fields_of(section: JsonObject) -> JsonObject:
    """The slots an extraction writes into: a section's `fields`, or the amendment details block itself."""
    return section.get("fields", section)


def slot_at(fields: JsonObject, path: str) -> object:
    node: object = fields
    for part in path.split("."):
        node = node[part]
    return node


def set_slot_at(fields: JsonObject, path: str, built: object) -> None:
    *parents, last = path.split(".")
    node = fields
    for part in parents:
        node = node[part]
    node[last] = built


@dataclass(frozen=True)
class ExpectedRow:
    """A row the table should have whenever the source mentions it; models sometimes leave such rows out."""
    name: str  # how the follow-up prompt asks for the row
    in_source: re.Pattern[str]  # source wording that shows the protocol covers it
    in_row: re.Pattern[str]  # matched against a row's key fields to tell whether the row is there


@dataclass
class Extraction:
    """What one prompt asks for, and where the answers go."""
    label: str
    section: str
    lead_numbers: tuple[str, ...]
    ask_for: str  # extra instruction, task-specific
    scalars: dict[str, JsonObject] = field(default_factory=dict)  # fields path -> description
    records: tuple[str, JsonObject] | None = None  # (fields key, list description) of record-per-row answers
    expected_rows: tuple[ExpectedRow, ...] = ()
    row_key_fields: tuple[str, ...] = ()  # the row fields that say which expected row a row is
    # fields a follow-up row must quote from the source; it was asked for, so it has to show the source has it
    required_evidence: tuple[str, ...] = ()


def extract(protocol: JsonObject, client: LlmClient, schema: JsonObject, validate: Validator,
            extraction: Extraction) -> JsonObject:
    """Return a copy of `protocol` with the extraction's answers applied; answers that don't validate are dropped."""
    result = copy.deepcopy(protocol)
    section = section_at(result, extraction.section)
    context = build_context(result, extraction.section, extraction.lead_numbers)
    filled, rejected = [], []
    if context.text and context.sources:
        prompt = build_prompt(extraction, context)
        answers = ask(prompt, client)
        if answers is None:
            rejected = [extraction.label]
        else:
            apply_scalars(result, section, extraction, answers, context, client, validate, filled, rejected)
            kept = apply_records(result, section, extraction, answers, context, client, validate, filled, rejected)
            ask_for_missing_rows(result, section, extraction, kept, context, client, validate, filled, rejected)
    log = result.setdefault("processing_log", [])
    assert isinstance(log, list)
    log.append({"step": f"extract_{extraction.label}", "actor": {"kind": "llm", "name": "converter.generate.fields",
                "model": client.model}, "at": now_iso(),
                "summary": f"{extraction.label}: filled {len(filled)}: {', '.join(filled) or 'none'}; "
                           f"rejected {len(rejected)}: {', '.join(rejected) or 'none'}"})
    return result


def build_prompt(extraction: Extraction, context: Context) -> str:
    lines = [f"- {prompt_line(path, description)}" for path, description in extraction.scalars.items()]
    if extraction.records:
        name, description = extraction.records
        lines.append(f"- {prompt_line(name, description)}")
    return (f"{INSTRUCTIONS}\n{extraction.ask_for}\n\n{format_block(extraction.section)}\n\n"
            "FIELDS (answer as one JSON object keyed by field name; each value is "
            '{"value": the answer, "confidence": "high" | "medium" | "low" | "very_low"}, except a list of rows, which is the list '
            'itself with a "confidence" key in each row object):\n'
            + "\n".join(lines) + (f"\n{EVIDENCE_INSTRUCTION}" if extraction.records else "")
            + f"\n\nSOURCE PROTOCOL (the parts related to this section):\n{context.text}\n")


def try_apply(protocol: JsonObject, section: JsonObject, apply: Callable[[], None], context: Context,
              client: LlmClient, validate: Validator) -> bool:
    """Run `apply`, mark the section, validate; undo everything on errors."""
    snapshot = copy.deepcopy(section)
    apply()
    mark_section_ai(section, context, client)
    if validate(protocol):
        section.clear()
        section.update(snapshot)
        return False
    return True


def apply_scalars(protocol, section, extraction, answers, context, client, validate, filled, rejected) -> None:
    for path, description in extraction.scalars.items():
        raw, answer = answer_value(answers.get(path))
        built = build_value(description, raw, with_confidence(context, answer), client)
        answered = has_content(built)
        if not answered:
            built = mark_no_basis(built, client)
        ok = try_apply(protocol, section, lambda: set_slot_at(fields_of(section), path, built), context, client, validate)
        if answered or not ok:
            (filled if ok else rejected).append(path)


def answered_rows(answers: JsonObject | None, name: str) -> list[object]:
    rows, _ = answer_value((answers or {}).get(name))  # the model sometimes wraps the row list like a single field
    return rows if isinstance(rows, list) else []


def apply_records(protocol, section, extraction, answers, context, client, validate, filled, rejected) -> list[object]:
    """Apply the answered rows; returns the rows kept."""
    if not extraction.records:
        return []
    name, _ = extraction.records
    return apply_rows(protocol, section, extraction, answered_rows(answers, name), [], name, context, client,
                      validate, filled, rejected)


def apply_rows(protocol, section, extraction, rows, kept, label, context, client, validate, filled, rejected,
               required_evidence: tuple[str, ...] = ()) -> list[object]:
    """Add each valid row after `kept`; returns the rows kept."""
    name, description = extraction.records
    item = description["item"]
    for index, row in enumerate(rows):
        built = build_value(item, row, with_confidence(context, row), client)
        if not has_content(built) or is_not_applicable_row(built, item):
            continue
        evidence = row.get(EVIDENCE_KEY) if isinstance(row, dict) else None
        flag_unsupported_fields(built, item, evidence, context.text)
        flag_fields_not_in_source(built, row.get(NOT_IN_SOURCE_KEY) if isinstance(row, dict) else None)
        if required_evidence and not quotes_required_fields(built, evidence, required_evidence):
            rejected.append(f"{label}[{index}]")
            continue
        candidate = [*kept, built]
        if try_apply(protocol, section, lambda: fields_of(section).__setitem__(name, candidate), context, client, validate):
            kept = candidate
            filled.append(f"{label}[{index}]")
        else:
            rejected.append(f"{label}[{index}]")
    return kept


def quotes_required_fields(row: JsonObject, evidence: object, required: tuple[str, ...]) -> bool:
    if not isinstance(evidence, dict):
        return False
    for name in required:
        slot = row.get(name)
        if not isinstance(slot, dict) or slot.get("status") != "filled" or slot["custody"].get("note") in REVIEW_NOTES:
            return False
    return True


def row_key_text(row: object, key_fields: tuple[str, ...]) -> str:
    texts = []
    for name in key_fields:
        slot = row.get(name) if isinstance(row, dict) else None
        value = slot.get("value") if isinstance(slot, dict) else None
        texts.append(str(value.get("decode", "")) if isinstance(value, dict) else str(value or ""))
    return " ".join(texts)


def missing_rows(extraction: Extraction, kept: list[object], source_text: str) -> list[ExpectedRow]:
    """Expected rows the source mentions but no kept row covers."""
    keys = [row_key_text(row, extraction.row_key_fields) for row in kept]
    return [expected for expected in extraction.expected_rows
            if expected.in_source.search(source_text) and not any(expected.in_row.search(key) for key in keys)]


def build_gap_prompt(extraction: Extraction, expected: ExpectedRow, context: Context) -> str:
    name, _ = extraction.records
    ask_for = (f"{name}: only the row for {expected.name}, as a list holding that one row. Answer "
               f'{{"{name}": []}} when the protocol sets no reporting rule for it.')
    return build_prompt(replace(extraction, ask_for=ask_for, scalars={}), context)


def ask_for_missing_rows(protocol, section, extraction, kept, context, client, validate, filled, rejected) -> None:
    """Ask once more for each expected row the first answer left out."""
    if not extraction.records:
        return
    name, _ = extraction.records
    for expected in missing_rows(extraction, kept, context.text):
        answers = ask(build_gap_prompt(extraction, expected, context), client)
        kept = apply_rows(protocol, section, extraction, answered_rows(answers, name), kept,
                          f"{name}:{expected.name}", context, client, validate, filled, rejected,
                          extraction.required_evidence)


def normalized_for_quote(text: str) -> str:
    text = text.translate(QUOTE_CHARACTERS).lower()
    return " ".join(text.split())


def is_supported(quote: object, value: str, source_text: str) -> bool:
    """Whether `quote` is found in the source verbatim and either holds the value itself or is long enough to say
    something."""
    if not isinstance(quote, str):
        return False
    quote = normalized_for_quote(quote).strip(" .\"'…")
    if not quote or quote not in normalized_for_quote(source_text):
        return False
    holds_value = normalized_for_quote(value).strip(" .") in quote
    return holds_value or len(quote.split()) >= MIN_EVIDENCE_WORDS


def flag_fields_not_in_source(row: JsonObject, names: object) -> None:
    """Mark the filled fields the model says it guessed (the source has no value for them) as very_low."""
    if not isinstance(names, list):
        return
    for name in names:
        slot = row.get(name) if isinstance(name, str) else None
        if isinstance(slot, dict) and slot.get("status") == "filled":
            slot["custody"]["confidence"] = "very_low"
            slot["custody"]["note"] = NOT_IN_SOURCE_NOTE


def flag_unsupported_fields(row: JsonObject, description: JsonObject, evidence: object, source_text: str) -> None:
    """Mark each filled text field of a row whose quote is missing or not in the source as a guess (very_low), so it
    gets reviewed even when the model rated the row well. A row with no evidence object at all is left unchecked."""
    if not isinstance(evidence, dict):
        return
    for name, child in description["fields"].items():
        slot = row.get(name)
        if child["kind"] != "text" or not isinstance(slot, dict) or slot.get("status") != "filled":
            continue
        if not is_supported(evidence.get(name), str(slot["value"]), source_text):
            slot["custody"]["confidence"] = "very_low"
            slot["custody"]["note"] = UNSUPPORTED_NOTE


def is_not_applicable_row(row: JsonObject, description: JsonObject) -> bool:
    """A row whose text fields all say "not applicable": it states that a kind of event or item does not apply, which
    M11 tables leave out rather than list."""
    texts = [row[name] for name, child in description["fields"].items() if child["kind"] == "text"]
    statuses = [slot["status"] for slot in texts if isinstance(slot, dict)]
    return "not_applicable" in statuses and "filled" not in statuses


# ---------------------------------------------------------------- what to extract

def navigate(description: JsonObject, path: str) -> JsonObject:
    for part in path.split("."):
        description = description["fields"][part]
    return description


def fields_description(schema: JsonObject, def_name: str) -> JsonObject:
    return describe(schema, schema["$defs"][def_name])


def overall_design_extraction(schema: JsonObject) -> Extraction:
    design = fields_description(schema, "Fields_1_1_2")
    paths = [
        "intervention_codes", "intervention_nonproprietary_names", "population_diagnosis_or_condition",
        "intervention_model", "population_type", "control_type", "control_description", "intervention_assignment_method",
        "randomisation_type", "other_assignment_method", "stratification", "site_distribution", "site_geographic_scope",
        "master_protocol", "drug_device_combination_product", "adaptive_trial_design", "number_of_arms",
        "trial_blind_schema", "blinded_roles", "participant_number_qualifier", "number_of_participants",
        "participant_number_basis", "intervention_duration", "participation_duration",
        "intervention_duration_description", "participation_duration_description", "independent_committees",
        "other_committees", "population_age.minimum", "population_age.minimum_unit", "population_age.maximum",
        "population_age.maximum_unit",
    ]
    return Extraction(
        "overall_design", "1.1.2", ("1.1", "1.1.1", "1.1.2", "4.1", "5.1", "5.2", "6.1"),
        "Fields of the Overall Design: read the trial's design, blinding, size, duration and age limits. Give a "
        "duration description (text) only when the duration is not a single number of units. Give "
        "randomisation_type only for randomised trials and other_assignment_method only when the method is Other. "
        f"Answer '{NOT_APPLICABLE_ANSWER}' for each of these when it does not apply. "
        "intervention_codes: the sponsor's investigational product codes; intervention_nonproprietary_names: their "
        "nonproprietary names (INN). population_diagnosis_or_condition: each condition studied, as a MedDRA preferred "
        "term where possible, or 'N/A – Healthy' for healthy volunteers; take it from the disease named in the "
        "title, indication or eligibility criteria. blinded_roles: the roles kept unaware of the treatment "
        "assignment (for example participant, investigator, outcomes assessor); when the trial is blinded but the "
        "source names no roles, suggest the roles that blinding implies and give a low confidence. population_age: the age limits in the "
        "inclusion criteria; when they set no upper age limit, answer population_age.maximum "
        f"'{NOT_APPLICABLE_ANSWER}' and population_age.maximum_unit '{NOT_APPLICABLE_ANSWER}'. master_protocol and "
        "adaptive_trial_design: "
        "'Yes' only when the source describes a master protocol (several sub-studies under one protocol) or "
        "prospectively planned adaptations based on interim data; otherwise 'No'. drug_device_combination_product: "
        "'Yes' when the investigational product is given with or through a device that is part of it; 'No' when the "
        "source says no medical device is used (for example a Medical Devices section reading 'Not applicable').",
        {path: navigate(design, path) for path in paths},
    )


def interventions_extraction(schema: JsonObject) -> Extraction:
    description = fields_description(schema, "Fields_6")["fields"]["interventions"]
    return Extraction(
        "interventions", "6", ("6", "6.1", "1.1.2", "4.1"),
        "interventions: one row for each combination of arm and trial intervention (drug, placebo, background "
        "therapy, device). Dose form and route: the term as the source writes it. dosage_levels: only the amount "
        "given at one administration, as a number and unit (for example '200 mg' or '20 mg/kg'), with no frequency, "
        "cycle count or duration. regimen: the frequency in its short standard form first (for example 'Q3W' or "
        "'once daily'), then any schedule detail.",
        records=("interventions", description),
    )


SAFETY_EXPECTED_ROWS = (
    ExpectedRow("Pregnancy Event", re.compile(r"pregnan", re.IGNORECASE), re.compile(r"pregnan", re.IGNORECASE)),
    ExpectedRow("Post-Partum Event", re.compile(r"post-?partum", re.IGNORECASE),
                re.compile(r"post-?partum", re.IGNORECASE)),
    ExpectedRow("Lactation Event", re.compile(r"breastfe|lactat", re.IGNORECASE),
                re.compile(r"lactat|breastfe", re.IGNORECASE)),
    ExpectedRow("Trial Intervention Complaint", re.compile(r"complaint", re.IGNORECASE),
                re.compile(r"complaint", re.IGNORECASE)),
)


def safety_reporting_extraction(schema: JsonObject) -> Extraction:
    description = fields_description(schema, "Fields_9_2")["fields"]["safety_event_reporting"]
    return Extraction(
        "safety_event_reporting", "9.2", ("9.2", "9.1", "9", "8.4"),
        "safety_event_reporting: one row for each kind of safety event the protocol says to report to the sponsor, "
        "with when reporting starts and ends, how fast, and by what method. Check each of these and give it its own "
        "row when the protocol sets reporting rules for it: Adverse Event, Serious Adverse Event, Reportable Adverse "
        "Event of Special Interest, Pregnancy Event, Post-Partum Event, Lactation Event and Trial Intervention "
        "Complaint.",
        records=("safety_event_reporting", description),
        expected_rows=SAFETY_EXPECTED_ROWS,
        row_key_fields=("event_type", "other_event_type"),
        required_evidence=("reportable_period_end", "timing_for_reporting"),
    )


def amendment_extraction(schema: JsonObject) -> Extraction:
    history = fields_description(schema, "Fields_12_3")["fields"]
    return Extraction(
        "amendment_history", "12.3", ("12.3",),
        "Amendment history: read it from the document history, summary of changes or title page. When the protocol "
        f"has not been amended, M11 has no prior-amendment table: answer '{NOT_APPLICABLE_ANSWER}' for "
        "overview_location and original_protocol_approval_date. For prior_amendments, one row per amendment already "
        "issued.",
        {name: history[name] for name in ("amendment_statement", "overview_location", "original_protocol_approval_date")},
        records=("prior_amendments", history["prior_amendments"]),
    )


def amendment_details_extraction(schema: JsonObject) -> Extraction:
    details = fields_description(schema, "AmendmentDetails")["fields"]
    paths = [
        "statement", "approximate_enrolled.quantity", "approximate_enrolled.unit", "enrollment_scope",
        "primary_reasons", "primary_reason_other", "secondary_reasons", "secondary_reason_other", "summary",
        "substantial_impact_on_safety_or_rights", "safety_or_rights_explanation",
        "substantial_impact_on_data_reliability", "data_reliability_explanation",
    ]
    return Extraction(
        "amendment_details", AMENDMENT_DETAILS, ("front", "12.3"),
        "Details of THIS amendment (the one this document is), read from its summary of changes, rationale or "
        f"title page. Answer '{NOT_APPLICABLE_ANSWER}' when the protocol is not an amendment. For changes, one row "
        "for each change: "
        "description, rationale, and the section it affects.",
        {path: navigate({"fields": details}, path) for path in paths},
        records=("changes", details["changes"]),
    )


def structured_extractions(schema: JsonObject) -> list[Extraction]:
    return [overall_design_extraction(schema), interventions_extraction(schema),
            safety_reporting_extraction(schema), amendment_extraction(schema),
            amendment_details_extraction(schema)]


def is_amendment(protocol: JsonObject) -> bool:
    """Whether the title page gives an amendment number; the renderer shows Amendment Details only then."""
    return amendment_state(render_values(protocol["title_page"])) != "none"


def extract_structured_fields(protocol: JsonObject, client: LlmClient, schema: JsonObject,
                              validate: Validator) -> JsonObject:
    result = protocol
    for extraction in structured_extractions(schema):
        # an original protocol has no current amendment; asking would only produce guessed details
        if extraction.section == AMENDMENT_DETAILS and not is_amendment(result):
            continue
        result = extract(result, client, schema, validate, extraction)
    result = extract_country_regions(result, client, schema, validate)
    result = extract_objectives(result, client, schema, validate)
    result = fill_summary_rows(result, client, validate)
    return extract_analyses(result, client, schema, validate)


# ---------------------------------------------------------------- 12.2 country and region requirements

COUNTRY_INSTRUCTIONS = """\
Country or region specific requirements: read only text the source gives for one named country or region
(for example "European Union", "Japan", "United States"). One entry per country or region:
  {"country_code": ISO 3166-1 alpha-3 code, "country_name": name, "requirements": [paragraph, ...],
   "clarifications": [paragraph, ...], "confidence": "high" | "medium" | "low" | "very_low"}
requirements are the specific requirements and clarifications are explanations of differences from the global
protocol. Copy the source's wording. Answer {"country_regions": []} when the source has no such text.
"""
ALPHA3 = re.compile(r"^[A-Z]{3}$")


def ai_block(text: str, context: Context, client: LlmClient) -> JsonObject:
    return {"type": "paragraph", "text": text.strip(), "style": "Normal", "custody": ai_custody(context, client)}


def country_region(entry: object, context: Context, client: LlmClient) -> JsonObject | None:
    if not isinstance(entry, dict):
        return None
    code, name = entry.get("country_code"), entry.get("country_name")
    if not (isinstance(code, str) and ALPHA3.match(code) and isinstance(name, str) and name.strip()):
        return None
    texts = {key: [text for text in entry.get(key) or [] if isinstance(text, str) and text.strip()]
             for key in ("requirements", "clarifications")}
    if not any(texts.values()):
        return None
    identifier = {"value": {"system": ISO_COUNTRY_SYSTEM, "code": code, "decode": name.strip()}, "status": "filled",
                  "custody": ai_custody(context, client)}
    return {"identifier": identifier,
            "requirements": [ai_block(text, context, client) for text in texts["requirements"]],
            "clarifications": [ai_block(text, context, client) for text in texts["clarifications"]],
            "custody": ai_custody(context, client)}


def extract_country_regions(protocol: JsonObject, client: LlmClient, schema: JsonObject,
                            validate: Validator) -> JsonObject:
    """Fill 12.2 from country-specific text found anywhere in the protocol; only when 12.2 itself is empty."""
    result = copy.deepcopy(protocol)
    section = section_at(result, "12.2")
    filled: list[str] = []
    if section["status"] == "missing":
        context = build_context(result, "12.2", ("12",))
        answers = ask(f"{INSTRUCTIONS}\n{COUNTRY_INSTRUCTIONS}\nAnswer as one JSON object.\n\n{format_block('12.2')}\n\n"
                      f"SOURCE PROTOCOL (the parts related to this section):\n{context.text}\n", client) \
            if context.sources else None
        regions: list[JsonObject] = []
        for entry in (answers or {}).get("country_regions") or []:
            region = country_region(entry, with_confidence(context, entry), client)
            if region is None:
                continue
            candidate = [*regions, region]
            if try_apply(result, section, lambda: section["fields"].__setitem__("country_regions", candidate),
                         context, client, validate):
                regions = candidate
                filled.append(region["identifier"]["value"]["code"])
        # M11: "If not applicable, retain the heading and enter 'Not applicable.'"
        if answers is not None and not regions and try_apply(
                result, section, lambda: section.__setitem__("status", "not_applicable"), context, client, validate):
            filled.append(NOT_APPLICABLE_ANSWER)
    log = result.setdefault("processing_log", [])
    assert isinstance(log, list)
    log.append({"step": "extract_country_regions", "actor": {"kind": "llm", "name": "converter.generate.fields",
                "model": client.model}, "at": now_iso(),
                "summary": f"country_regions: filled {len(filled)}: {', '.join(filled) or 'none'}"})
    return result


# ---------------------------------------------------------------- 3 objectives and estimands

OBJECTIVE_KINDS = {"primary": "3.1", "secondary": "3.2", "exploratory": "3.3"}
OBJECTIVE_SOURCE_SECTIONS = ("1.1.1", "10.4", "10.5")  # the analysis sections often define the estimands
OBJECTIVE_INSTRUCTIONS = """\
Trial objectives, endpoints and estimands (ICH E9(R1)): copy the source's wording. Answer one JSON object:
{"primary": [objective, ...], "secondary": [...], "exploratory": [...]}
where each objective is
  {"category": "Efficacy"/"Safety"/etc., "statement": the objective as written,
   "confidence": "high" | "medium" | "low" | "very_low",
   "estimands": [{"population": text, "treatment": text, "endpoint": the endpoint(s) that measure this objective,
     "population_level_summary": text, "intercurrent_events": [{"description": text, "strategy": text}]}]}
Give one estimand per objective (more only when the source defines several) and fill every characteristic. When
the source does not state one, give the most likely value from what it does say; set the objective's confidence to
"very_low" when the source gives no basis for it. Use [] for a kind with no objectives.
"""
TEXT_DESCRIPTION: JsonObject = {"kind": "text"}


def text_slot(raw: object, context: Context, client: LlmClient) -> JsonObject:
    return build_slot(TEXT_DESCRIPTION, raw, context, client)


def build_estimand(kind: str, index: int, number: int, entry: object, context: Context, client: LlmClient) -> JsonObject:
    entry = entry if isinstance(entry, dict) else {}
    events = [{"description": text_slot(event.get("description"), context, client),
               "strategy": text_slot(event.get("strategy"), context, client)}
              for event in entry.get("intercurrent_events") or []
              if isinstance(event, dict) and has_content(text_slot(event.get("description"), context, client))]
    return {"id": f"{kind}-{index}-estimand-{number}",
            **{name: text_slot(entry.get(name), context, client)
               for name in ("population", "treatment", "endpoint", "population_level_summary")},
            "intercurrent_events": events, "custody": ai_custody(context, client)}


def build_objective(kind: str, index: int, entry: object, context: Context, client: LlmClient) -> JsonObject | None:
    if not isinstance(entry, dict) or not has_content(text_slot(entry.get("statement"), context, client)):
        return None
    estimands = [build_estimand(kind, index, number, estimand, context, client)
                 for number, estimand in enumerate(entry.get("estimands") or [{}], start=1)]
    return {"id": f"{kind}-{index}", "number": f"{OBJECTIVE_KINDS[kind]}.{index}",
            "title": f"{kind.title()} Objective {index}",
            "category_label": text_slot(entry.get("category"), context, client),
            "statement": text_slot(entry["statement"], context, client),
            "estimands": estimands or [build_estimand(kind, index, 1, {}, context, client)],
            "additional_attributes": [], "content": [], "heading_source": None,
            "custody": ai_custody(context, client)}


def extract_objectives(protocol: JsonObject, client: LlmClient, schema: JsonObject, validate: Validator) -> JsonObject:
    """Fill the 3.1-3.3 objectives; the source text they replace is dropped from that section's extra headings."""
    result = copy.deepcopy(protocol)
    context = build_context(result, "3", OBJECTIVE_SOURCE_SECTIONS)
    answers = ask(f"{INSTRUCTIONS}\n{OBJECTIVE_INSTRUCTIONS}\n\n{format_block('3.1.1')}\n\n"
                  f"SOURCE PROTOCOL (the parts related to this section):\n{context.text}\n", client) \
        if context.sources else None
    filled: list[str] = []
    for kind, number in OBJECTIVE_KINDS.items():
        section = section_at(result, number)
        entries = (answers or {}).get(kind)
        objectives = [build_objective(kind, index, entry, with_confidence(context, entry), client)
                      for index, entry in enumerate(entries if isinstance(entries, list) else [], start=1)]
        objectives = [objective for objective in objectives if objective]
        if objectives and try_apply(result, section, lambda: apply_objectives(section, objectives), context, client, validate):
            filled.append(f"{kind}[{len(objectives)}]")
    log = result.setdefault("processing_log", [])
    assert isinstance(log, list)
    log.append({"step": "extract_objectives", "actor": {"kind": "llm", "name": "converter.generate.fields",
                "model": client.model}, "at": now_iso(),
                "summary": f"objectives: filled {len(filled)}: {', '.join(filled) or 'none'}"})
    return result


def apply_objectives(section: JsonObject, objectives: list[JsonObject]) -> None:
    section["objectives"] = objectives
    section["status"] = "container"
    section["additional_subsections"] = []


# ---------------------------------------------------------------- 1.1.1 synopsis rows

ESTIMAND_SUMMARY_PARTS = (("Population", "population"), ("Treatment", "treatment"), ("Endpoint", "endpoint"),
                          ("Summary", "population_level_summary"))
SYNOPSIS_KINDS = ("primary", "secondary")


def estimand_summary(objective: JsonObject) -> str:
    parts = []
    for estimand in objective["estimands"]:
        parts.extend(f"{label}: {estimand[name]['value']}" for label, name in ESTIMAND_SUMMARY_PARTS
                     if estimand[name]["status"] == "filled")
    return "\n".join(parts)


def summary_row(objective: JsonObject, context: Context, client: LlmClient) -> JsonObject:
    summary = estimand_summary(objective)
    context = with_confidence(context, objective["custody"])
    return {"objective_id": objective["id"], "objective": copy.deepcopy(objective["statement"]),
            "estimand_summary": text_slot(summary, context, client) if summary else missing_slot()}


def fill_summary_rows(protocol: JsonObject, client: LlmClient, validate: Validator) -> JsonObject:
    """1.1.1 lists the primary and secondary objectives from section 3 with a summary of each estimand."""
    result = copy.deepcopy(protocol)
    objectives = [objective for kind in SYNOPSIS_KINDS
                  for objective in section_at(result, OBJECTIVE_KINDS[kind]).get("objectives", [])]
    section = section_at(result, "1.1.1")
    context = Context(sources=merge_sources([section["custody"].get("v10_sources", []),
                                             *(objective["custody"]["v10_sources"] for objective in objectives)]))
    rows = [summary_row(objective, context, client) for objective in objectives]
    if rows:
        try_apply(result, section, lambda: section["fields"].__setitem__("summary_rows", rows), context, client, validate)
    return result


# ---------------------------------------------------------------- 10.4 / 10.5 analyses per objective

ANALYSIS_SECTIONS = {"primary": "10.4", "secondary": "10.5"}
ANALYSIS_PARTS = ("statistical_analysis_method", "handling_of_data", "handling_of_missing_data",
                  "sensitivity_analysis", "supplementary_analysis")
OPTIONAL_ANALYSIS = "_Optional"
ANALYSIS_FORMAT_SECTIONS = tuple(f"10.4.X.{part}" for part in range(1, len(ANALYSIS_PARTS) + 1))
ANALYSIS_INSTRUCTIONS = """\
Statistical analyses for each objective below. Copy the source's wording; do not summarise or invent.
Answer one JSON object keyed by objective id. Each value is
  {"statistical_analysis_method": heading, "handling_of_data": heading, "handling_of_missing_data": heading,
   "sensitivity_analysis": heading, "supplementary_analysis": heading}
where each heading is {"paragraphs": [paragraph, ...], "confidence": "high" | "medium" | "low" | "very_low"} with the
source text that belongs under it. Judge each heading's confidence on its own. When the source has no text for a
heading but what it says about that objective's analysis reasonably implies the content, write it and set that
heading's confidence to "very_low". When the source gives no basis even for a reasonable inference, do not guess:
answer that heading as {"status": "no_basis", "paragraphs": []}. When the source describes the analyses of several objectives together, give the text to each objective it
covers.
"""


def analysis_titles(kind: str) -> dict[str, str]:
    """The M11 headings under each objective's analysis."""
    estimands = f"{kind.title()} Estimand(s)"
    return {
        "statistical_analysis_method": "Statistical Analysis Method",
        "handling_of_data": f"Handling of Data in Relation to {estimands}",
        "handling_of_missing_data": f"Handling of Missing Data in Relation to {estimands}",
        "sensitivity_analysis": "Sensitivity Analysis",
        "supplementary_analysis": "Supplementary Analysis",
    }


def analysis_is_optional(schema: JsonObject, section: str, name: str) -> bool:
    definition = schema["$defs"][f"ObjectiveAnalysis_{section.replace('.', '_')}"]
    return definition["properties"]["subsections"]["properties"][name]["$ref"].endswith(OPTIONAL_ANALYSIS)


def build_analysis(schema: JsonObject, section: str, position: int, objective: JsonObject, kind: str,
                   answer: object, context: Context, client: LlmClient) -> JsonObject:
    answer = answer if isinstance(answer, dict) else {}
    titles = analysis_titles(kind)
    parts: JsonObject = {}
    for index, name in enumerate(ANALYSIS_PARTS, start=1):
        heading = answer.get(name)
        has_own_confidence = isinstance(heading, dict) and "confidence" in heading
        part_context = with_confidence(context, heading) if has_own_confidence else context
        texts = heading.get("paragraphs") if isinstance(heading, dict) else heading
        paragraphs = [text for text in texts or [] if isinstance(text, str) and text.strip()]
        optional = analysis_is_optional(schema, section, name)
        no_basis = isinstance(heading, dict) and heading.get("status") == NO_BASIS
        if paragraphs and not no_basis:
            status, content, custody = "populated", [ai_block(text, part_context, client) for text in paragraphs], \
                ai_custody(part_context, client)
        elif no_basis and not optional:
            # a required heading the model could not write: left for a person, and marked as such when rendered
            status, content = "missing", []
            custody = ai_custody(replace(context, confidence=NO_BASIS_CONFIDENCE), client)
        else:
            status = "omitted" if optional else "missing"
            content, custody = [], missing_slot()["custody"]
        parts[name] = {"number": f"{section}.{position}.{index}", "title": titles[name], "status": status,
                       "heading_source": None, "custody": custody, "content": content}
    populated = any(part["status"] == "populated" for part in parts.values())
    return {"objective_id": objective["id"], "number": f"{section}.{position}",
            "title": f"{kind.title()} Objective {position}", "status": "populated" if populated else "missing",
            "heading_source": None, "custody": ai_custody(context, client) if populated else missing_slot()["custody"],
            "content": [], "subsections": parts}


def extract_analyses(protocol: JsonObject, client: LlmClient, schema: JsonObject, validate: Validator) -> JsonObject:
    """Fill 10.4 and 10.5 with one analysis per section 3 objective, from the source's analysis text.

    Text the analyses replace is dropped from the section's extra headings, as for the objectives.
    """
    result = copy.deepcopy(protocol)
    wanted = {kind: section_at(result, OBJECTIVE_KINDS[kind]).get("objectives", []) for kind in ANALYSIS_SECTIONS}
    objective_lines = [f'- {objective["id"]}: {objective["statement"]["value"]}'
                       for objectives in wanted.values() for objective in objectives
                       if objective["statement"]["status"] == "filled"]
    context = build_context(result, "10")
    answers = None
    if objective_lines and context.sources:
        prompt = (f"{INSTRUCTIONS}\n{ANALYSIS_INSTRUCTIONS}\n{format_block(*ANALYSIS_FORMAT_SECTIONS)}\n\n"
                  "OBJECTIVES:\n" + "\n".join(objective_lines)
                  + f"\n\nSOURCE PROTOCOL (the parts related to this section):\n{context.text}\n")
        answers = ask(prompt, client)
    filled: list[str] = []
    for kind, number in ANALYSIS_SECTIONS.items():
        section = section_at(result, number)
        analyses = [build_analysis(schema, number, position, objective, kind, (answers or {}).get(objective["id"]),
                                   with_confidence(context, (answers or {}).get(objective["id"])), client)
                    for position, objective in enumerate(wanted[kind], start=1)]
        if any(analysis["status"] == "populated" for analysis in analyses) \
                and try_apply(result, section, lambda: apply_analyses(section, analyses), context, client, validate):
            filled.append(f"{number}[{len(analyses)}]")
    log = result.setdefault("processing_log", [])
    assert isinstance(log, list)
    log.append({"step": "extract_analyses", "actor": {"kind": "llm", "name": "converter.generate.fields",
                "model": client.model}, "at": now_iso(),
                "summary": f"analyses: filled {len(filled)}: {', '.join(filled) or 'none'}"})
    return result


def apply_analyses(section: JsonObject, analyses: list[JsonObject]) -> None:
    section["objective_analyses"] = analyses
    section["additional_subsections"] = []
