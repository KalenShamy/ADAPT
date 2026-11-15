"""Choose the source content related to one M11 section, for that section's LLM prompt.

Every prompt used to get the whole protocol cut at a character limit. Instead, each section gets, in priority order:
  0. its own placed content, and the synopsis (1.1);
  1. content from the v010 sections the crosswalk and the schema expect to feed it, wherever that was placed;
  2. sections linked to it by an M11 cross-reference, in either direction;
  3. keyword matches on its title, M11 element names and labels, anywhere in the protocol;
  4. content around what tiers 1-3 picked: the neighbouring blocks and the parent section's lead-in.
Each chosen block records its tier and reason, so a reviewer can see why it was included.
"""

import copy
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from converter.generate.guidance import REPEAT_PART, format_lines, section_format, template_key, template_sections
from restructure import load_crosswalk

type JsonObject = dict[str, object]

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CROSSWALK = ROOT / "resources" / "research" / "crosswalk.tsv"
ANNOTATED_SCHEMA = ROOT / "schema" / "protocol.schema.annotated.json"

CONTEXT_CEILING_CHARS = 30000
SYNOPSIS_MAX_CHARS = 6000
RELATED_MAX_CHARS = 12000
SYNOPSIS_SECTION = "1.1"
FRONT_LOCATION = "front"
MAX_KEYWORD_HITS = 6
MIN_KEYWORD_SCORE = 6.0  # BM25; lower scores were mostly a shared generic word ("laboratory", "sample")
HEADING_OVERHEAD_CHARS = 5  # the "\n## " and line break around each heading
NEIGHBOUR_BLOCKS = 1
PARENT_LEAD_IN_BLOCKS = 2
CROSS_REFERENCE_MAX_CHARS = 5000  # a heading-only section referred to whole when its subsections are this short
OPENING_BLOCKS = 2  # otherwise this many blocks from the start of each of its subsections
BM25_K1 = 1.5
BM25_B = 0.75
STOP_WORDS = frozenset({
    "a", "an", "and", "or", "of", "the", "for", "to", "in", "on", "with", "by", "at", "from", "as", "is", "are", "be",
    "will", "this", "that", "any", "all", "if", "enter", "other", "trial", "study",
})

OWN, SYNOPSIS, EXPECTED, CROSS_REFERENCED, KEYWORD, SURROUNDING = range(6)
TIER_NAMES = {OWN: "own content", SYNOPSIS: "synopsis", EXPECTED: "expected source", CROSS_REFERENCED:
              "cross-referenced", KEYWORD: "keyword match", SURROUNDING: "surrounding"}


def block_lines(blocks: list[JsonObject]) -> list[str]:
    lines = []
    for block in blocks:
        if block["type"] == "table":
            lines.extend(" | ".join(row) for row in block["rows"])
        elif block.get("text"):
            lines.append(block["text"])
    return lines


def merge_sources(source_lists: list[list[JsonObject]]) -> list[JsonObject]:
    merged: dict[str, JsonObject] = {}
    for sources in source_lists:
        for source in sources:
            existing = merged.setdefault(source["section_id"], copy.deepcopy(source))
            existing["source_indices"] = sorted(set(existing["source_indices"]) | set(source["source_indices"]))
    return list(merged.values())


def words(text: str) -> list[str]:
    return [word for word in re.findall(r"[a-z0-9]+", text.lower()) if word not in STOP_WORDS]


def within(location: str, number: str) -> bool:
    return location == number or location.startswith(number + ".")


def parent_number(number: str) -> str | None:
    return number.rsplit(".", 1)[0] if "." in number else None


# ---------------------------------------------------------------- what the crosswalk and schema expect

@cache
def expected_v10_sections(crosswalk_path: Path = DEFAULT_CROSSWALK) -> dict[str, frozenset[str]]:
    """M11 number -> the v010 section ids ("v10:6.1") that should feed it."""
    expected: dict[str, set[str]] = {}
    for row in load_crosswalk(crosswalk_path).values():
        for target in [row.target, *row.also]:
            if target:
                expected.setdefault(target, set()).add(f"v10:{row.number}")
    definitions = json.loads(ANNOTATED_SCHEMA.read_text())["$defs"]
    for name, definition in definitions.items():
        if not name.startswith("Section_") or "__" in name:
            continue
        number = name.removeprefix("Section_").replace("_", ".")
        for source in definition.get("x-v10-default-sources", []):
            expected.setdefault(number, set()).add(source["section_id"])
    return {number: frozenset(ids) for number, ids in expected.items()}


@cache
def v10_primary_targets(crosswalk_path: Path = DEFAULT_CROSSWALK) -> dict[str, str]:
    """v010 section id -> the M11 section its content is placed in."""
    return {f"v10:{row.number}": row.target for row in load_crosswalk(crosswalk_path).values() if row.target}


@cache
def incoming_cross_refs() -> dict[str, frozenset[str]]:
    """M11 key -> the M11 keys whose template text refers to it."""
    incoming: dict[str, set[str]] = {}
    for key, entry in template_sections().items():
        for reference in entry["cross_refs"]:
            incoming.setdefault(reference, set()).add(key)
    return {key: frozenset(sources) for key, sources in incoming.items()}


# ---------------------------------------------------------------- the protocol as blocks

@dataclass
class Unit:
    order: int
    location: str  # number of the numbered section that holds it, or "front"
    heading: str  # what the prompt shows above it
    text: str
    sources: list[JsonObject]

    @property
    def section_ids(self) -> set[str]:
        return {str(source["section_id"]) for source in self.sources}


@dataclass
class SourceIndex:
    units: list[Unit]
    by_location: dict[str, list[Unit]]

    @classmethod
    def build(cls, protocol: JsonObject) -> "SourceIndex":
        units: list[Unit] = []

        def add(location: str, heading: str, blocks: list[JsonObject], node_sources: list[JsonObject]) -> None:
            for block in blocks:
                lines = block_lines([block])
                if lines:
                    sources = block.get("custody", {}).get("v10_sources") or node_sources
                    units.append(Unit(len(units), location, heading, "\n".join(lines), sources))

        def visit(node: JsonObject, number: str, heading: str) -> None:
            if node.get("status") in (None, "populated"):
                add(number, heading, node.get("content", []), node.get("custody", {}).get("v10_sources", []))
            for extra in node.get("additional_subsections", []):
                visit(extra, number, f"{heading} > {extra.get('title', '')}")
            children = node.get("subsections")
            for key, child in (children.items() if isinstance(children, dict) else []):
                child_number = str(child.get("number") or key)
                visit(child, child_number, f"{child_number} {child.get('title', '')}".strip())

        for key, section in protocol["sections"].items():
            visit(section, key, f"{key} {section.get('title', '')}".strip())
        for item in protocol.get("unplaced_v10_content", []):
            heading = item["v10_source"].get("found_heading") or "front matter"
            add(FRONT_LOCATION, f"front matter > {heading}", item["blocks"], [item["v10_source"]])
        by_location: dict[str, list[Unit]] = {}
        for unit in units:
            by_location.setdefault(unit.location, []).append(unit)
        return cls(units, by_location)

    def in_section(self, number: str) -> list[Unit]:
        return [unit for unit in self.units if within(unit.location, number)]

    def directly_in(self, number: str) -> list[Unit]:
        """The section's own blocks and its additional subsections, without its numbered subsections."""
        return self.by_location.get(number, [])

    def own(self, number: str) -> list[Unit]:
        """What the section holds: its direct content, or for a heading-only section, everything under it."""
        if section_format(number).heading_only or not self.directly_in(number):
            return self.in_section(number)
        return self.directly_in(number)

    def neighbours(self, unit: Unit) -> list[Unit]:
        siblings = self.by_location[unit.location]
        position = siblings.index(unit)
        return siblings[max(0, position - NEIGHBOUR_BLOCKS):position] + siblings[position + 1:position + 1 + NEIGHBOUR_BLOCKS]

    def lead_in(self, number: str) -> list[Unit]:
        """The first blocks of the nearest ancestor-or-self of `number` that has text of its own."""
        current: str | None = number
        while current and not self.directly_in(current):
            current = parent_number(current)
        return self.directly_in(current)[:PARENT_LEAD_IN_BLOCKS] if current else []

    def referenced(self, key: str) -> list[Unit]:
        """What a cross-reference to `key` brings in. A section's own text when it has some, so "align with
        Section 7" does not pull in the whole chapter. A heading-only section ("Section 9.3") has its content in its
        subsections: all of it when short, else the opening blocks of each subsection."""
        direct = self.directly_in_key(key)
        if direct or REPEAT_PART in key.split("."):
            return direct
        below = self.in_section(key)
        if sum(len(unit.text) for unit in below) <= CROSS_REFERENCE_MAX_CHARS:
            return below
        locations = dict.fromkeys(unit.location for unit in below)
        return [unit for location in locations for unit in self.directly_in(location)[:OPENING_BLOCKS]]

    def directly_in_key(self, key: str) -> list[Unit]:
        """directly_in for an M11 key, where a repeating key ("10.4.X.2") matches every number it stands for."""
        if REPEAT_PART not in key.split("."):
            return self.directly_in(key)
        pattern = re.compile(re.escape(key).replace(re.escape(REPEAT_PART), r"\d+") + "$")
        return [unit for location, units in self.by_location.items() if pattern.match(location) for unit in units]


# ---------------------------------------------------------------- keyword search

def keyword_terms(number: str, title: str) -> list[str]:
    section = section_format(number)
    texts = [title or section.title]
    texts += [str(element.get("element_name", "")) for element in section.elements]
    texts += [str(element.get("ich_preferred_term") or "") for element in section.elements]
    texts += [str(item.get("text", "")) for item in section.items if item["kind"] == "label"]
    return list(dict.fromkeys(word for text in texts for word in words(text)))


def nearest_heading(unit: Unit) -> str:
    return unit.heading.rsplit(" > ", 1)[-1]


def bm25_scores(units: list[Unit], terms: list[str]) -> dict[int, float]:
    if not units or not terms:
        return {}
    # the nearest heading counts too: a block reading only "Not applicable." is about what its heading names
    documents = {unit.order: Counter(words(f"{nearest_heading(unit)} {unit.text}")) for unit in units}
    lengths = {order: sum(counts.values()) for order, counts in documents.items()}
    average_length = sum(lengths.values()) / len(lengths) or 1
    frequency = Counter(term for counts in documents.values() for term in set(counts) if term in terms)
    scores: dict[int, float] = {}
    for order, counts in documents.items():
        score = 0.0
        for term in terms:
            if not counts[term]:
                continue
            idf = math.log(1 + (len(documents) - frequency[term] + 0.5) / (frequency[term] + 0.5))
            weight = counts[term] * (BM25_K1 + 1)
            norm = counts[term] + BM25_K1 * (1 - BM25_B + BM25_B * lengths[order] / average_length)
            score += idf * weight / norm
        if score:
            scores[order] = score
    return scores


# ---------------------------------------------------------------- choosing and writing the context

@dataclass
class Pick:
    unit: Unit
    tier: int
    reason: str


@dataclass
class SectionContext:
    number: str
    text: str = ""
    sources: list[JsonObject] = field(default_factory=list)
    picks: list[Pick] = field(default_factory=list)
    own_blocks_left_out: int = 0

    def as_json(self) -> JsonObject:
        return {
            "chars": len(self.text),
            "own_blocks_left_out": self.own_blocks_left_out,
            "picks": [{"tier": TIER_NAMES[pick.tier], "reason": pick.reason, "location": pick.unit.location,
                       "heading": pick.unit.heading, "chars": len(pick.unit.text),
                       "preview": pick.unit.text[:120]} for pick in self.picks],
        }


def cross_references(number: str) -> list[str]:
    """M11 keys this section refers to or is referred to by. A heading-only section holds its subsections' content,
    so their references count too."""
    keys = template_sections()
    own_key = template_key(number, keys) or number
    covered = [own_key]
    if section_format(number).heading_only:
        covered += [key for key in keys if within(key, own_key) and key != own_key]
    references: list[str] = []
    for key in covered:
        references += section_format(key).cross_refs
        references += sorted(incoming_cross_refs().get(key, ()))
    return [reference for reference in dict.fromkeys(references) if not within(reference, own_key)]


def candidates(index: SourceIndex, number: str, title: str, seeds: tuple[str, ...]) -> list[Pick]:
    """Every block that could go in the context, in tier order (a block appears once, at its best tier)."""
    picks: list[Pick] = []
    chosen: set[int] = set()

    def offer(units: list[Unit], tier: int, reason: str) -> None:
        for unit in units:
            if unit.order not in chosen:
                chosen.add(unit.order)
                picks.append(Pick(unit, tier, reason))

    offer(index.own(number), OWN, number)
    if not within(number, SYNOPSIS_SECTION):
        offer(index.in_section(SYNOPSIS_SECTION), SYNOPSIS, SYNOPSIS_SECTION)

    # a subsection also takes what its ancestors expect: v10 6.1 feeds 6.9, and 6.9.1 is where that text belongs
    current: str | None = number.split("#")[0]
    while current:
        expected = expected_v10_sections().get(current, frozenset())
        suffix = "" if current == number.split("#")[0] else f" (expected for {current})"
        for unit in index.units:
            shared = unit.section_ids & expected
            if shared:
                offer([unit], EXPECTED, f"from {', '.join(sorted(shared))}{suffix}")
        for section_id in sorted(expected):
            target = v10_primary_targets().get(section_id)
            # placed in this section or the ancestor itself: own text, or the ancestor's lead-in (added below)
            if target and not within(target, number) and not within(target, current):
                offer(index.directly_in(target), EXPECTED, f"{section_id} is placed in {target}{suffix}")
        current = parent_number(current)
    for seed in seeds:
        offer(index.in_section(seed), EXPECTED, f"{seed} is a known source for this section")

    for reference in cross_references(number):
        offer(index.referenced(reference), CROSS_REFERENCED, f"M11 cross-reference {reference}")

    scores = bm25_scores([unit for unit in index.units if unit.order not in chosen], keyword_terms(number, title))
    best = sorted(scores.items(), key=lambda item: -item[1])[:MAX_KEYWORD_HITS]
    for order, score in best:
        if score >= MIN_KEYWORD_SCORE:
            offer([index.units[order]], KEYWORD, f"keywords, score {score:.1f}")

    for unit in [pick.unit for pick in picks if pick.tier in (EXPECTED, CROSS_REFERENCED, KEYWORD)]:
        offer(index.neighbours(unit), SURROUNDING, f"next to a block from {unit.location}")
    parent = parent_number(number)
    if parent:
        offer(index.lead_in(parent), SURROUNDING, f"lead-in of {parent}")
    return picks


def section_context(index: SourceIndex, number: str, title: str = "", seeds: tuple[str, ...] = ()) -> SectionContext:
    picks = candidates(index, number, title, seeds)
    kept: list[Pick] = []
    used = {OWN: 0, SYNOPSIS: 0, "related": 0}
    own_left_out = 0
    for pick in picks:
        size = len(pick.unit.text) + len(pick.unit.heading) + HEADING_OVERHEAD_CHARS  # counts its heading as if new
        total = sum(used.values())
        if pick.tier == OWN:
            fits = total + size <= CONTEXT_CEILING_CHARS - SYNOPSIS_MAX_CHARS
            own_left_out += not fits
            group = OWN
        elif pick.tier == SYNOPSIS:
            fits = used[SYNOPSIS] + size <= SYNOPSIS_MAX_CHARS
            group = SYNOPSIS
        else:
            fits = used["related"] + size <= RELATED_MAX_CHARS and total + size <= CONTEXT_CEILING_CHARS
            group = "related"
        if fits:
            used[group] += size
            kept.append(pick)
    kept.sort(key=lambda pick: pick.unit.order)
    return SectionContext(number, render(kept), merge_sources([pick.unit.sources for pick in kept]), kept, own_left_out)


def render(picks: list[Pick]) -> str:
    parts: list[str] = []
    heading = None
    for pick in picks:
        if pick.unit.heading != heading:
            heading = pick.unit.heading
            parts.append(f"\n## {heading}")
        parts.append(pick.unit.text)
    return "\n".join(parts).strip()


def all_section_contexts(protocol: JsonObject) -> dict[str, JsonObject]:
    """The context and M11 format of every section in the protocol, filled or not, keyed by number."""
    index = SourceIndex.build(protocol)
    result: dict[str, JsonObject] = {}

    def visit(node: JsonObject, number: str) -> None:
        context = section_context(index, number, str(node.get("title", "")))
        section = section_format(number)
        result[number] = {"title": node.get("title"), "status": node.get("status"),
                          "format": {"m11_key": section.number, "lines": format_lines(section)},
                          **context.as_json(), "text": context.text}
        children = node.get("subsections")
        for key, child in (children.items() if isinstance(children, dict) else []):
            visit(child, str(child.get("number") or key))

    for key, section in protocol["sections"].items():
        visit(section, key)
    return result
