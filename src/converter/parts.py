"""Tag the placed 9.2.2 Collection Procedures blocks with the M11 element each belongs to.

M11 splits 9.2.2 into Identification, Severity, Causality, Recording and Follow-up. A v010 protocol holds the same
text under its own subheadings ("Assessment of Intensity", "Recording and Follow-Up of AE and/or SAE"), so each
block takes the part named by the nearest subheading above it, or else by the v010 heading it was placed from.
"""

import re

type JsonObject = dict[str, object]

COLLECTION_SECTION_PATH = ("9", "9.2", "9.2.2")
# Checked in order; a heading naming two parts ("Recording and Follow-Up") takes the first match
PART_KEYWORDS = [
    ("Identification", re.compile(r"\b(detect|identif)", re.IGNORECASE)),
    ("Severity", re.compile(r"\b(intensity|severity)", re.IGNORECASE)),
    ("Causality", re.compile(r"\bcausal", re.IGNORECASE)),
    ("Recording", re.compile(r"\brecord", re.IGNORECASE)),
    ("Follow-up", re.compile(r"\bfollow[\s-]?up", re.IGNORECASE)),
]
COLLECTION_PARTS = [part for part, _ in PART_KEYWORDS]
MAX_SUBHEADING_WORDS = 8
SENTENCE_END = (".", ":", ";", ",")


def tag_collection_parts(protocol: JsonObject) -> list[str]:
    """Set `part` on the 9.2.2 blocks whose part can be told; return a report note."""
    section = protocol["sections"][COLLECTION_SECTION_PATH[0]]
    for number in COLLECTION_SECTION_PATH[1:]:
        section = section["subsections"][number]
    current = None
    tagged = 0
    for block in section["content"]:
        if is_subheading(block):
            current = part_named(block["text"]) or current
        part = current or part_named(source_heading(block))
        if part:
            block["part"] = part
            tagged += 1
    return [f"9.2.2 blocks sorted into M11 parts: {tagged} of {len(section['content'])}"]


def part_named(text: str) -> str | None:
    return next((part for part, pattern in PART_KEYWORDS if pattern.search(text)), None)


def is_subheading(block: JsonObject) -> bool:
    if block["type"] == "heading":
        return True
    if block["type"] != "paragraph":
        return False
    text = block["text"].strip()
    return 0 < len(text.split()) <= MAX_SUBHEADING_WORDS and not text.endswith(SENTENCE_END)


def source_heading(block: JsonObject) -> str:
    sources = block.get("custody", {}).get("v10_sources", [])
    return " ".join(source.get("found_heading") or "" for source in sources)
