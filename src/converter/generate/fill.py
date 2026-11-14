"""Fill `missing` sections of a protocol JSON by asking an LLM to draft them from the source text."""

import copy
import json
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

from converter.generate.client import LlmClient, LlmError
from converter.generate.prompts import build_prompt, parse_response

type JsonObject = dict[str, object]
type SectionValidator = Callable[[JsonObject], list[str]]

MAX_ATTEMPTS = 2
GENERATED_STATUSES = ("populated", "not_applicable", "missing")
SCRIPT_NAME = "converter.generate.fill"


def walk_sections(sections: dict[str, JsonObject]) -> Iterator[JsonObject]:
    for section in sections.values():
        yield section
        children = section.get("subsections")
        if isinstance(children, dict):
            yield from walk_sections(children)


def missing_sections(protocol: JsonObject) -> list[JsonObject]:
    sections = protocol["sections"]
    assert isinstance(sections, dict)
    return [section for section in walk_sections(sections) if section.get("status") == "missing"]


def source_text(protocol: JsonObject) -> str:
    """Plain text of every populated section, headed by number and title."""
    sections = protocol["sections"]
    assert isinstance(sections, dict)
    parts: list[str] = []
    for section in walk_sections(sections):
        if section.get("status") != "populated":
            continue
        body = block_texts(section.get("content", []))
        parts.append(f"## {section['number']} {section['title']}\n" + "\n".join(body))
    return "\n\n".join(parts)


def block_texts(blocks: object) -> list[str]:
    texts: list[str] = []
    for block in blocks if isinstance(blocks, list) else []:
        if block.get("type") == "table":
            texts.extend(" | ".join(row) for row in block["rows"])
        else:
            texts.append(block.get("text", ""))
    return texts


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def llm_custody(client: LlmClient, confidence: str) -> JsonObject:
    return {
        "origin": "ai_generated",
        "v10_sources": [],
        "actor": {"kind": "llm", "name": "claude", "model": client.model},
        "confidence": confidence,
        "needs_review": True,
    }


def apply_draft(section: JsonObject, draft: JsonObject, client: LlmClient) -> None:
    status = draft.get("status")
    if status not in GENERATED_STATUSES:
        raise ValueError(f"bad status {status!r}")
    if status == "missing":
        return
    confidence = draft.get("confidence", "low")
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    paragraphs = draft.get("paragraphs", [])
    if status == "populated" and not (isinstance(paragraphs, list) and paragraphs and all(isinstance(p, str) and p.strip() for p in paragraphs)):
        raise ValueError("populated draft has no paragraphs")
    custody = llm_custody(client, str(confidence))
    section["status"] = status
    section["custody"] = custody
    section["content"] = [
        {"type": "paragraph", "text": text.strip(), "style": "Normal", "custody": copy.deepcopy(custody)}
        for text in (paragraphs if status == "populated" else [])
    ]


def generate_missing(
    protocol: JsonObject, client: LlmClient, validate_section: SectionValidator | None = None
) -> JsonObject:
    """Return a copy of `protocol` with missing sections drafted. Failures leave the section missing and are logged."""
    result = copy.deepcopy(protocol)
    context = source_text(result)
    filled, left = [], []
    for section in missing_sections(result):
        number, title = str(section["number"]), str(section["title"])
        guidance = [str(line) for line in section.get("x-guidance", [])]
        prompt = build_prompt(number, title, guidance, context)
        if try_fill(section, prompt, client, validate_section):
            filled.append(number)
        else:
            left.append(number)
    log = result.setdefault("processing_log", [])
    assert isinstance(log, list)
    log.append({
        "step": "generate_missing_sections",
        "actor": {"kind": "llm", "name": SCRIPT_NAME, "model": client.model},
        "at": now_iso(),
        "summary": f"drafted {len(filled)} section(s): {', '.join(filled) or 'none'}; "
                   f"left missing {len(left)}: {', '.join(left) or 'none'}",
    })
    return result


def try_fill(section: JsonObject, prompt: str, client: LlmClient, validate_section: SectionValidator | None) -> bool:
    for _ in range(MAX_ATTEMPTS):
        snapshot = copy.deepcopy(section)
        try:
            apply_draft(section, parse_response(client.complete(prompt)), client)
        except (LlmError, ValueError, json.JSONDecodeError):
            restore(section, snapshot)
            continue
        if section["status"] == "missing":
            return False
        if validate_section is None or not validate_section(section):
            return True
        restore(section, snapshot)
    return False


def restore(section: JsonObject, snapshot: JsonObject) -> None:
    section.clear()
    section.update(snapshot)
