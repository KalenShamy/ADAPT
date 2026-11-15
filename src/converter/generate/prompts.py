"""Prompt for drafting one v011 section that has no v010 source."""

import json

from converter.protocol_fields import CONFIDENCE_LEVELS

MAX_DRAFT_WORDS = 250  # about half a single-spaced page

INSTRUCTIONS = """\
You are drafting one section of a clinical trial protocol in the TransCelerate CPT v011 / ICH M11 format.
Base the section on the SOURCE PROTOCOL below: what it states, or what it reasonably implies. When the source only
supports a reasonable inference, write it and set confidence to "very_low". When the source gives no basis even for a
reasonable inference, do not guess: answer with status "no_basis" and no paragraphs. Never invent a number,
name, dose, date or procedure the source does not give: write a bracketed placeholder instead (for example "[dose]").
If the source clearly says the topic does not apply, or the section is about a feature this trial does not have (for
example a rationale for an adaptive design in a trial with no adaptations), answer with status "not_applicable".

What to write:
- Follow the ICH M11 FORMAT for this section: its guidance says what the section is for and what belongs elsewhere,
  and its fixed text, labels and data elements say what the section must contain.
- Write the section's own content. Do not restate the objectives, endpoints, estimands or design summary unless the
  guidance asks for it. Point to another section by number (for example "see Section 6.2") instead of repeating it.
- A rationale needs a reason. A reason is a purpose, goal or background the source ties to that design choice (for
  example "a placebo add-on design to test X on top of standard of care"). Use those and say them plainly. A list of
  design features is not a reason: if the source only describes what the trial does and gives nothing that
  explains the choice, write the most likely reason that fits the source and set confidence to "very_low".
- For a "Rationale for Other Trial Design Aspects" style section, write only about a design aspect that the
  earlier rationale sections do not cover and the source explains. Do not recap visits, procedures or adjudication.
- Silence is not a fact. Never write that something is absent, not planned or not used unless the source says so.
- Write as finished protocol text: third person, present or future tense ("Participants will be randomized..."), the
  source's own terms and abbreviations. Never mention "the source", "the text" or "the protocol provided".
- Do not repeat the section heading, add citations the source does not contain, or pad with general background.

Answer with one JSON object and nothing else:
{
  "status": "populated" | "not_applicable" | "no_basis",
  "paragraphs": ["plain text paragraph", ...],
  "basis": ["source section number or heading each fact came from", ...],
  "confidence": "high" | "medium" | "low" | "very_low"
}
Use "paragraphs": [] unless status is "populated". Plain text only, no markdown, no bullet characters.
Keep the whole draft under {max_words} words (half a page) in total across all paragraphs. Be concise and
keep only what this section needs.
Confidence: "high" when the source states the content directly, "medium" when you combined facts from several
places, "low" when you inferred it from what the source says, "very_low" when the text is a reasonable inference the
source only thinly supports.
""".replace("{max_words}", str(MAX_DRAFT_WORDS))


def build_prompt(
    number: str, title: str, format_lines: list[str], source_text: str, example: str | None = None,
    subsection_titles: list[str] | None = None,
) -> str:
    """`source_text` is the section's chosen context (converter.generate.context), already within budget."""
    format_text = "\n".join(format_lines) or "(none)"
    example_block = (
        f"STYLE EXAMPLE, from a different trial. Copy its tone and structure only, none of its facts:\n{example}\n\n"
        if example else ""
    )
    subsection_block = (
        f"SUBSECTIONS THAT ALREADY HOLD THE DETAIL: {', '.join(subsection_titles)}.\n"
        "Write only a short overview of one or two sentences that introduces them. Do not repeat or "
        "paraphrase their content.\n\n"
        if subsection_titles else ""
    )
    return (
        f"{INSTRUCTIONS}\n"
        f"SECTION TO DRAFT: {number} {title}\n\n"
        f"{subsection_block}"
        f"ICH M11 FORMAT FOR THIS SECTION:\n{format_text}\n\n"
        f"{example_block}"
        f"SOURCE PROTOCOL (the parts related to this section):\n{source_text}\n"
    )


def parse_response(text: str) -> dict[str, object]:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in response")
    parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("response JSON is not an object")
    return parsed
