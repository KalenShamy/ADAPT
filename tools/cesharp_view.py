"""Build a one-file HTML view of the CeSHarP (ICH M11) data a protocol .docx encodes.

Imports the docx to USDM v4 with usdm4_protocol's M11 importer, then writes an HTML page showing what
the importer read (identifiers, objectives, estimands, interventions, section tree) and what it could
not (importer warnings and errors, empty design elements).
Needs Python 3.12 and the root requirements.txt.

Usage: python tools/cesharp_view.py <file.docx> [--out view.html]
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

TEMPLATES = {
    "report": Path(__file__).with_name("cesharp_view_template.html"),
    "diagram": Path(__file__).with_name("cesharp_diagram_template.html"),
}
DATA_PLACEHOLDER = "/*CESHARP_DATA*/null"
SECTION_REFERENCE = re.compile(r"section\s+(\d+(?:\.\d+)*)", re.IGNORECASE)
SHOWN_LEVELS = {"Warning", "Error"}

DESIGN_ELEMENTS = (
    "arms", "epochs", "encounters", "activities", "scheduleTimelines",
    "eligibilityCriteria", "objectives", "estimands", "analysisPopulations", "indications",
)
VERSION_ELEMENTS = (
    "studyIdentifiers", "titles", "studyInterventions", "abbreviations", "organizations", "roles",
    "eligibilityCriterionItems", "narrativeContentItems",
)


def import_to_usdm(docx: Path) -> tuple[dict, list[dict]]:
    from simple_error_log.errors import Errors
    from usdm4_protocol import USDM4M11

    with tempfile.TemporaryDirectory() as work_dir:
        importer = USDM4M11()
        wrapper = importer.from_docx(str(docx), work_dir=work_dir)
        if wrapper is None:
            raise SystemExit(f"The M11 importer could not read {docx.name}.\n{importer.errors.dump().strip()}")
        usdm = json.loads(wrapper.model_dump_json())
        rows = importer.errors.to_dict(level=Errors.DEBUG)
    return usdm, rows


def code_text(code: dict | None) -> str:
    return (code or {}).get("decode", "")


def section_tree(usdm: dict, issues: list[dict]) -> list[dict]:
    version = usdm["study"]["versions"][0]
    text_by_item = {item["id"]: item.get("text", "") for item in version["narrativeContentItems"]}
    contents = usdm["study"]["documentedBy"][0]["versions"][0]["contents"]
    issues_by_section: dict[str, int] = {}
    for issue in issues:
        for number in SECTION_REFERENCE.findall(issue["message"]):
            issues_by_section[number] = issues_by_section.get(number, 0) + 1
    return [
        {
            "number": content["sectionNumber"],
            "title": content["sectionTitle"],
            "chars": len(text_by_item.get(content.get("contentItemId"), "")),
            "issues": issues_by_section.get(content["sectionNumber"], 0),
        }
        for content in contents
        if content["sectionNumber"] or content["sectionTitle"]
    ]


def objective_tree(design: dict) -> list[dict]:
    return [
        {
            "level": code_text(objective.get("level")),
            "text": objective["text"],
            "endpoints": [
                {"level": code_text(endpoint.get("level")), "text": endpoint["text"]}
                for endpoint in objective.get("endpoints", [])
            ],
        }
        for objective in design["objectives"]
    ]


def build_view(usdm: dict, rows: list[dict], docx_name: str) -> dict:
    version = usdm["study"]["versions"][0]
    design = version["studyDesigns"][0]
    issues = [
        {"level": row["level"], "message": row["message"]}
        for row in rows
        if row["level"] in SHOWN_LEVELS and "Ignoring eTree element" not in row["message"]
    ]
    counts = {name: len(design.get(name) or []) for name in DESIGN_ELEMENTS}
    counts.update({name: len(version.get(name) or []) for name in VERSION_ELEMENTS})
    counts["endpoints"] = sum(len(objective.get("endpoints", [])) for objective in design["objectives"])
    return {
        "file": docx_name,
        "usdmVersion": usdm["usdmVersion"],
        "studyName": usdm["study"]["name"],
        "titles": [{"type": code_text(title["type"]), "text": title["text"]} for title in version["titles"]],
        "counts": counts,
        "objectives": objective_tree(design),
        "estimands": [
            {"name": estimand["name"], "population": estimand.get("populationSummary", "")}
            for estimand in design["estimands"]
        ],
        "populations": [{"name": pop["name"], "text": pop.get("text", "")} for pop in design["analysisPopulations"]],
        "interventions": [
            {"name": item["name"], "role": code_text(item.get("role")), "type": code_text(item.get("type"))}
            for item in version["studyInterventions"]
        ],
        "sections": section_tree(usdm, issues),
        "issues": issues,
    }


def render_html(view: dict, template: Path) -> str:
    data = json.dumps(view, ensure_ascii=False).replace("</", "<\\/")
    return template.read_text(encoding="utf-8").replace(DATA_PLACEHOLDER, data)


def main() -> int:
    if not sys.flags.utf8_mode:
        return subprocess.run([sys.executable, "-X", "utf8", *sys.argv]).returncode
    parser = argparse.ArgumentParser(description="HTML view of the CeSHarP data in a protocol .docx")
    parser.add_argument("file", type=Path)
    parser.add_argument("--view", choices=sorted(TEMPLATES), default="report",
                        help="report: tabs of extracted data; diagram: one-page docx-to-USDM map")
    parser.add_argument("--out", type=Path, help="output HTML (default: <docx stem>.cesharp.html beside the docx)")
    args = parser.parse_args()
    if args.file.suffix.lower() != ".docx" or not args.file.is_file():
        print(f"Not a .docx file: {args.file}", file=sys.stderr)
        return 2
    usdm, rows = import_to_usdm(args.file)
    out_path = args.out or args.file.with_name(f"{args.file.stem}.cesharp.html")
    html = render_html(build_view(usdm, rows, args.file.name), TEMPLATES[args.view])
    out_path.write_text(html, encoding="utf-8")
    print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
