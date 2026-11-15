"""Convert v010 protocol PDFs or Word files to v011: extract, place, render, LLM-generate missing sections, render again."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from converter.export.fhir import export_fhir
from converter.extract.pdf import load_pdf
from converter.generate.client import DEFAULT_BACKEND, make_client
from converter.generate.context import all_section_contexts
from converter.generate.fields import extract_structured_fields
from converter.generate.fill import generate_missing
from converter.generate.table_titles import title_untitled_tables
from converter.omit import omit_empty_optional
from converter.place import place_document
from converter.render.docx import render_docx
from converter.validate import load_schema, validate_document
from restructure import load_crosswalk, template_outline



RESOURCES = Path(__file__).resolve().parents[2] / "resources"
DEFAULT_TEMPLATE = RESOURCES / "CPT_CoreBWE-v011.docx"
DEFAULT_CROSSWALK = RESOURCES / "research" / "crosswalk.tsv"
TOOLS = RESOURCES.parent / "tools"
DOCX_VALIDATORS = (
    ("validate_m11", "m11", "validate_m11.py"),
    ("validate_cesharp", "cesharp", "validate_cesharp.py"),
)


PDF_SUFFIX = ".pdf"
DOCX_SUFFIX = ".docx"
SUPPORTED_SUFFIXES = (PDF_SUFFIX, DOCX_SUFFIX)


def load_docx(path: Path):
    """Imported on first use so PDF conversion works without the docx extractor."""
    from converter.extract.docx import load_docx as load
    return load(path)


def load_source(path: Path):
    """Extract the Document with the loader that matches the file extension."""
    suffix = path.suffix.lower()
    if suffix == PDF_SUFFIX:
        return load_pdf(path)
    if suffix == DOCX_SUFFIX:
        return load_docx(path)
    raise ValueError(f"{path}: unsupported input type {path.suffix!r}; expected one of {', '.join(SUPPORTED_SUFFIXES)}")


class ValidationFailed(RuntimeError):
    pass


def crosswalk_targets(crosswalk: dict) -> frozenset[str]:
    """v011 section numbers that some v010 section maps to."""
    return frozenset(
        number for row in crosswalk.values() for number in [row.target, *row.also] if number
    )


def check(protocol: dict[str, object], stage: str) -> None:
    errors, _ = validate_document(protocol)
    if errors:
        shown = "\n".join(errors[:10])
        raise ValidationFailed(f"{stage} JSON failed schema validation ({len(errors)} errors):\n{shown}")


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def run_docx_validators(docx: Path, validators: dict[str, Path]) -> list[str]:
    """Run each tools/ validator on docx, save its full report beside it, and return summary lines.

    The validators need usdm4_protocol, so they run in a child process instead of being imported.
    """
    lines = []
    for label, script in validators.items():
        completed = subprocess.run([sys.executable, str(script), str(docx)], capture_output=True, text=True,
                                   encoding="utf-8")
        report_path = docx.with_suffix(f".{label}.txt")
        report_path.write_text(completed.stdout or completed.stderr, encoding="utf-8")
        summary = [line for line in completed.stdout.splitlines() if line.startswith(("Findings:", "RESULT:"))]
        lines.append(f"{label} validation of {docx.name}: " + "; ".join(summary or [completed.stderr.strip()]))
        lines.append(f"{label} validation report: {report_path.name}")
    return lines


def convert_file(source_path: Path, out_dir: Path, template: Path, crosswalk_path: Path,
                  backend: str | None, model: str | None, dump_context: bool = False,
                  validators: dict[str, Path] | None = None, fhir: bool = False) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = source_path.stem
    assets_dir = out_dir / f"{name}.assets"
    schema = load_schema()
    crosswalk = load_crosswalk(crosswalk_path)
    source = load_source(source_path)
    protocol, layout, report = place_document(
        source, source_path, schema, crosswalk, template_outline(template),
        assets_dir, str(template.relative_to(RESOURCES.parent)),
    )
    check(protocol, "placed")
    write_json(out_dir / f"{name}.json", protocol)
    write_json(out_dir / f"{name}.layout.json", layout)
    render_docx(protocol, layout, assets_dir, template, out_dir / f"{name}.pre-llm.docx", name)
    if dump_context:
        write_json(out_dir / f"{name}.context.json", all_section_contexts(protocol))
    lines = [f"{name}: placed {len(layout)} blocks", *report.unmatched, *report.notes,
             *(f"docx: {note}" for note in getattr(source, "notes", []))]
    if backend is not None:
        client = make_client(backend, model)
        def schema_errors(candidate: dict[str, object]) -> list[str]:
            return validate_document(candidate)[0]

        generated = generate_missing(protocol, client, schema_errors, schema, crosswalk_targets(crosswalk))
        generated = extract_structured_fields(generated, client, schema, schema_errors)
        title_untitled_tables(generated, client)
        omit_empty_optional(generated, schema)
        check(generated, "generated")
        write_json(out_dir / f"{name}.generated.json", generated)
        render_docx(generated, layout, assets_dir, template, out_dir / f"{name}.docx", name)
        lines.extend(entry["summary"] for entry in generated["processing_log"][-8:])
        if fhir:
            exported = export_fhir(generated, layout, assets_dir)
            write_json(out_dir / f"{name}.fhir.json", exported.bundle)
            lines.extend(exported.notes)
    if validators:
        final_docx = out_dir / f"{name}.docx" if backend is not None else out_dir / f"{name}.pre-llm.docx"
        lines.extend(run_docx_validators(final_docx, validators))
    (out_dir / f"{name}.report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+", help="v010 protocols (.pdf or .docx)")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--llm", choices=["cli", "codex", "api"], default=DEFAULT_BACKEND)
    parser.add_argument("--model", help="model name for the chosen backend")
    parser.add_argument("--skip-generate", action="store_true", help="stop after the pre-LLM docx")
    parser.add_argument("--fhir", action="store_true",
                        help="also write <name>.fhir.json, a FHIR document Bundle built from the generated JSON")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--crosswalk", type=Path, default=DEFAULT_CROSSWALK)
    parser.add_argument("--dump-context", action="store_true",
                        help="write <name>.context.json: each section's M11 format and the source content chosen for it")
    parser.add_argument("--validate-m11", action="store_true",
                        help="check the output docx against the ICH M11 rules (tools/validate_m11.py)")
    parser.add_argument("--validate-cesharp", action="store_true",
                        help="check the output docx's CeSHarP structure via USDM (tools/validate_cesharp.py)")
    args = parser.parse_args()
    if args.fhir and args.skip_generate:
        parser.error("--fhir exports the generated JSON, so it needs the generate step; drop --skip-generate")
    backend = None if args.skip_generate else args.llm
    validators = {label: TOOLS / script for flag, label, script in DOCX_VALIDATORS if getattr(args, flag)}
    for path in args.inputs:
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise SystemExit(f"{path}: unsupported input type {path.suffix!r}; expected .pdf or .docx")
        for line in convert_file(path, args.out_dir, args.template, args.crosswalk, backend, args.model,
                                 args.dump_context, validators, args.fhir):
            print(line)


if __name__ == "__main__":
    main()
