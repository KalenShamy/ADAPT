"""Ask the LLM to name each source table that has no caption, so the renderer can caption every table."""

import json
from collections.abc import Iterator

from converter.generate.client import LlmClient, LlmError
from converter.generate.fill import now_iso
from converter.generate.prompts import parse_response
from converter.place import SOURCE_TABLE_CAPTION, captioned_tables, is_table_block

type JsonObject = dict[str, object]

SCRIPT_NAME = "converter.generate.table_titles"
MAX_SAMPLE_ROWS = 3
MAX_SAMPLE_COLUMNS = 12
MAX_CELL_CHARS = 80
MAX_TEXT_ABOVE_CHARS = 300

INSTRUCTIONS = """\
You are naming tables in a clinical trial protocol in the ICH M11 format. The source protocol gave none of the
tables below a caption. Write a short title for each, in title case, the way a protocol captions a table (for example
"Schedule of Activities", "Objectives and Endpoints", "Study Arms"). Say what the table holds, using the protocol's
own terms. Do not number the title or start it with "Table".

Answer with one JSON object and nothing else:
{"titles": {"<table id>": "<title>", ...}}

TABLES:
"""


def title_untitled_tables(protocol: JsonObject, client: LlmClient) -> None:
    """Set `title` on each table that has neither a source caption above it nor a title; one LLM call for all."""
    tables = untitled_tables(protocol)
    titled = 0
    if tables:
        prompt = INSTRUCTIONS + "\n".join(describe(str(number), *table) for number, table in enumerate(tables, 1))
        try:
            answer = parse_response(client.complete(prompt))
        except (LlmError, ValueError, json.JSONDecodeError):
            answer = {}
        titles = answer.get("titles")
        titles = titles if isinstance(titles, dict) else {}
        for number, (table, _, _) in enumerate(tables, 1):
            title = clean_title(titles.get(str(number)))
            if title:
                table["title"] = title
                titled += 1
    log = protocol.setdefault("processing_log", [])
    assert isinstance(log, list)
    log.append({
        "step": "title_tables",
        "actor": {"kind": "llm", "name": SCRIPT_NAME, "model": client.model},
        "at": now_iso(),
        "summary": f"titled {titled} of {len(tables)} uncaptioned table(s)",
    })


def untitled_tables(protocol: JsonObject) -> list[tuple[JsonObject, str, str]]:
    """(table, heading of the section holding it, text of the paragraph above it) for each table still to name."""
    captioned = {id(table) for _, table in captioned_tables(protocol)}
    return [(table, heading, above) for table, heading, above in tables_under(protocol, "")
            if id(table) not in captioned and not table.get("title")]


def tables_under(node: object, heading: str) -> Iterator[tuple[JsonObject, str, str]]:
    if isinstance(node, dict):
        heading = node_heading(node) or heading
        for value in node.values():
            yield from tables_under(value, heading)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            if is_table_block(item):
                above = node[index - 1] if index else {}
                yield item, heading, str(above.get("text", "")) if isinstance(above, dict) else ""
            else:
                yield from tables_under(item, heading)


def node_heading(node: JsonObject) -> str:
    """'6.1 Description of Trial Intervention' for a section, the source heading for unplaced front matter."""
    title = node.get("title")
    if isinstance(title, str) and "content" in node:
        return f"{node.get('number') or ''} {title}".strip()
    source = node.get("v10_source")
    if "blocks" in node and isinstance(source, dict):
        return str(source.get("found_heading") or "")
    return ""


def describe(table_id: str, table: JsonObject, heading: str, above: str) -> str:
    rows = [" | ".join(cell[:MAX_CELL_CHARS] for cell in row[:MAX_SAMPLE_COLUMNS])
            for row in table["rows"][:MAX_SAMPLE_ROWS]]
    lines = [f"[{table_id}] in section: {heading or '(none)'}"]
    if above:
        lines.append(f"Text above the table: {above[:MAX_TEXT_ABOVE_CHARS]}")
    lines.extend(["First rows:", *rows, ""])
    return "\n".join(lines)


def clean_title(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    title = raw.strip().rstrip(".")
    numbered = SOURCE_TABLE_CAPTION.match(title)
    return numbered.group(2).strip() if numbered else title
