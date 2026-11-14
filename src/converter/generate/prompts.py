"""Prompt for drafting one v011 section that has no v010 source."""

import json

MAX_CONTEXT_CHARS = 24000

INSTRUCTIONS = """\
You are drafting one section of a clinical trial protocol in the TransCelerate CPT v011 / ICH M11 format.
Draft only from facts stated in the SOURCE PROTOCOL below. Do not invent numbers, names, doses, dates or
procedures. If the source gives no basis for this section, answer with status "missing".
If the source clearly says the topic does not apply, answer with status "not_applicable".

Answer with one JSON object and nothing else:
{
  "status": "populated" | "not_applicable" | "missing",
  "paragraphs": ["plain text paragraph", ...],
  "basis": ["source section number or heading each fact came from", ...],
  "confidence": "high" | "medium" | "low"
}
Use "paragraphs": [] unless status is "populated". Plain text only, no markdown.
"""


def build_prompt(number: str, title: str, guidance: list[str], source_text: str) -> str:
    trimmed = source_text[:MAX_CONTEXT_CHARS]
    guidance_text = "\n".join(f"- {line}" for line in guidance) or "(none)"
    return (
        f"{INSTRUCTIONS}\n"
        f"SECTION TO DRAFT: {number} {title}\n\n"
        f"TEMPLATE GUIDANCE FOR THIS SECTION:\n{guidance_text}\n\n"
        f"SOURCE PROTOCOL:\n{trimmed}\n"
    )


def parse_response(text: str) -> dict[str, object]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("response JSON is not an object")
    return parsed
