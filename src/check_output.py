"""Check converted v011 .docx files against the v011 template.

Usage:
    python check_output.py [--template T] out/*.docx

Checks: heading outline (L1/L2), styles exist in the template, no instructional text left,
cross-references point at existing headings. Exits 1 if any check fails.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import docx
from docx.oxml.ns import qn

from cross_references import OTHER_DOCUMENT, REFERENCE, replacements_for
from pdf_to_cpt_docx import STYLE_NAMES
from restructure import template_outline

DEFAULT_TEMPLATE = Path(__file__).resolve().parent.parent / "resources" / "CPT_CoreBWE-v011.docx"
INSTRUCTIONAL_STYLES = {"CPT_Instructional", "CPT_Example"}
MAX_SHOWN = 8


def output_outline(document):
    counters, outline = [], []
    heading_styles = STYLE_NAMES["v011"]["headings"]
    for paragraph in document.paragraphs:
        style = paragraph.style.name
        if style not in heading_styles:
            continue
        level = heading_styles.index(style) + 1
        del counters[level:]
        while len(counters) < level:
            counters.append(0)
        counters[-1] += 1
        outline.append((level, ".".join(map(str, counters)), paragraph.text.strip()))
    return outline


def check_outline(document, template_outline_rows, audit=None):
    problems = []
    output = [(level, number) for level, number, _ in output_outline(document) if level <= 2]
    expected = [(row.level, row.number) for row in template_outline_rows if row.level <= 2]
    rows = output_outline(document)
    if audit:
        if rows != [tuple(row) for row in audit["outline"]]:
            problems.append("output heading order/numbers/titles differ from writer audit")
        extras = {item["number"] for item in audit["extra_headings"]}
    else:
        extras = set()
    actual = [(level, number, title) for level, number, title in rows if level <= 2 and number not in extras]
    expected_rows = [row for row in template_outline_rows if row.level <= 2]
    if len(actual) != len(expected_rows):
        problems.append(f"template L1/L2 count {len(actual)} != {len(expected_rows)} (excluding reported extras)")
    for found, reference in zip(actual, expected_rows):
        level, number, title = found
        if level != reference.level:
            problems.append(f"{number}: heading level differs from template {reference.number}")
        expected_title = re.sub(r"\s+", " ", reference.title).strip()
        if "[" in expected_title:
            # resolve_template_title may substitute the complete source title for bracketed template options.
            resolved = next((row[2] for row in audit["outline"] if row[1] == number), None) if audit else None
            allowed = resolved or re.sub(r"\s+", " ", expected_title.replace("[#]", "").replace("[", "").replace("]", "")).strip()
        else:
            allowed = expected_title
        if re.sub(r"\s+", " ", title).strip() != allowed:
            problems.append(f"{number}: title {title!r} != template {allowed!r}")
    return problems, len(output), len(expected)


def check_styles(document, template):
    known = {style.name for style in template.styles}
    used = {p.style.name for p in document.paragraphs}
    for table in document.tables:
        used.add(table.style.name if table.style else "")
        for row in table.rows:
            for cell in row.cells:
                used.update(p.style.name for p in cell.paragraphs)
    problems = []
    unknown = sorted(name for name in used if name and name not in known)
    if unknown:
        problems.append(f"styles not in template: {unknown}")
    left = sorted(used & INSTRUCTIONAL_STYLES)
    if left:
        problems.append(f"instructional styles used: {left}")
    for paragraph in all_paragraphs(document):
        if re.search(r"<(Start|End) of (common|suggested|example) text>", paragraph.text):
            problems.append(f"template flag text left: {paragraph.text[:60]}")
            break
    return problems


def check_cross_references(document):
    numbers = {number for _, number, _ in output_outline(document)}
    bad = []
    for paragraph in all_paragraphs(document):
        for match in REFERENCE.finditer(paragraph.text):
            if OTHER_DOCUMENT.match(paragraph.text, match.end()):
                continue
            for replacement in replacements_for(match):
                for number in replacement.source_numbers:
                    if number not in numbers:
                        bad.append(f"{match.group(0)} (missing {number})")
    return [f"{len(bad)} references to missing sections, e.g. {bad[:MAX_SHOWN]}"] if bad else []


def all_paragraphs(document):
    yield from document.paragraphs
    for table in document.tables:
        seen = set()
        for row in table.rows:
            for cell in row.cells:
                if cell._tc not in seen:
                    seen.add(cell._tc)
                    yield from cell.paragraphs


def check_content(document, audit):
    if not audit:
        return ["missing writer audit: cannot verify narrative content"]
    expected = {entry["id"]: entry for entry in audit["blocks"]}
    found = {}
    active = None
    for element in document.element.body:
        if element.tag == qn("w:bookmarkStart") and element.get(qn("w:name"), "").startswith("source_block_"):
            active = element.get(qn("w:id"))
            if active in found:
                return [f"duplicate source block bookmark {active}"]
            found[active] = {"text": [], "tables": 0, "figures": 0}
        elif active and element.tag == qn("w:bookmarkEnd") and element.get(qn("w:id")) == active:
            active = None
        elif active:
            texts = ["".join(text.text or "" for text in paragraph.iter(qn("w:t")))
                     for paragraph in element.iter(qn("w:p"))]
            found[active]["text"].extend(" ".join(texts).split())
            found[active]["tables"] += int(element.tag == qn("w:tbl"))
            found[active]["figures"] += len(list(element.iter(qn("w:drawing"))))
    problems = []
    if set(found) != set(expected):
        problems.append("missing or unexpected source block bookmarks")
    for bookmark, entry in expected.items():
        actual = found.get(bookmark)
        if actual and any(actual[key] != entry[key] for key in ("text", "tables", "figures")):
            problems.append(f"source block {bookmark} ({entry['type']}): text/table/figure mismatch")
    if audit["source"]["figures"] != audit["written"]["figures"]:
        problems.append("source figure count differs from written figure count")
    if audit.get("unexplained_removals"):
        problems.append(f"{len(audit['unexplained_removals'])} narrative paragraphs removed outside synopsis filtering")
    tables = audit.get("tables")
    if tables:
        for index, source in enumerate(tables["sources"]):
            copies = sum(index in origins for origins in tables["outputs"])
            if copies == 0 and source["section"] != "1.1":
                problems.append(f"source table {index} p{source['page']} lost outside synopsis filtering")
            elif copies > 1 and source["section"] != "3":
                problems.append(f"source table {index} duplicated outside objectives splitting")
        if any(not origins for origins in tables["outputs"]):
            problems.append("written table has no source provenance")
    if audit["bookmark_mismatches"]:
        problems.append(f"{len(audit['bookmark_mismatches'])} source bookmark mismatches (listed in report)")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("outputs", nargs="+", type=Path)
    args = parser.parse_args()
    template = docx.Document(str(args.template))
    outline = template_outline(str(args.template))
    failed = False
    for path in args.outputs:
        document = docx.Document(str(path))
        audit_path = path.with_suffix(".audit.json")
        audit = json.loads(audit_path.read_text()) if audit_path.exists() else None
        outline_problems, out_count, expected_count = check_outline(document, outline, audit)
        problems = outline_problems + check_styles(document, template) + check_cross_references(document) + check_content(document, audit)
        status = "FAIL" if problems else "ok"
        failed |= bool(problems)
        print(f"{status}  {path.name}  (L1/L2 headings: {out_count}, template: {expected_count})")
        for problem in problems:
            print(f"      {problem}")
        report_path = path.with_suffix(".report.txt")
        if report_path.exists():
            text = report_path.read_text(encoding="utf-8").split("\nOutput verification\n")[0]
            text += "\nOutput verification\n" + ("\n".join(problems) if problems else "All automated DOCX checks passed") + "\n"
            if any("references to missing sections" in problem for problem in problems):
                text += "Unresolved references remain for review: the detected source outline did not supply every required target. No replacement target was guessed; see the cross-reference entries above.\n"
            if audit and audit["bookmark_mismatches"]:
                text += "Bookmark differences are extraction discrepancies, not omitted validation checks. Compare the reported source pages and titles; scanned pages have no OCR and cannot supply text headings.\n"
            text += "Manual check still required: open in Word, update the TOC, and inspect pagination and layout against the v011 examples.\n"
            report_path.write_text(text, encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
