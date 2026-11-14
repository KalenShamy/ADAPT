"""Convert v010 protocol PDFs to v011: extract, place, render, LLM-generate missing sections, render again."""

import argparse
import json
from pathlib import Path

from converter.extract.pdf import load_pdf
from converter.generate.client import make_client
from converter.generate.fill import generate_missing
from converter.place import place_document
from converter.render.docx import render_docx
from converter.validate import load_schema, validate_document
from restructure import load_crosswalk, template_outline

RESOURCES = Path(__file__).resolve().parents[2] / "resources"
DEFAULT_TEMPLATE = RESOURCES / "CPT_CoreBWE-v011.docx"
DEFAULT_CROSSWALK = RESOURCES / "research" / "crosswalk.tsv"


class ValidationFailed(RuntimeError):
    pass


def check(protocol: dict[str, object], stage: str) -> None:
    errors, _ = validate_document(protocol)
    if errors:
        shown = "\n".join(errors[:10])
        raise ValidationFailed(f"{stage} JSON failed schema validation ({len(errors)} errors):\n{shown}")


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


def convert_pdf(pdf_path: Path, out_dir: Path, template: Path, crosswalk_path: Path,
                backend: str | None, model: str | None) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    name = pdf_path.stem
    assets_dir = out_dir / f"{name}.assets"
    schema = load_schema()
    protocol, layout, report = place_document(
        load_pdf(pdf_path), pdf_path, schema, load_crosswalk(crosswalk_path), template_outline(template),
        assets_dir, str(template.relative_to(RESOURCES.parent)),
    )
    check(protocol, "placed")
    write_json(out_dir / f"{name}.json", protocol)
    write_json(out_dir / f"{name}.layout.json", layout)
    render_docx(protocol, layout, assets_dir, template, out_dir / f"{name}.pre-llm.docx", name)
    lines = [f"{name}: placed {len(layout)} blocks", *report.unmatched, *report.notes]
    if backend is not None:
        client = make_client(backend, model)
        generated = generate_missing(protocol, client)
        check(generated, "generated")
        write_json(out_dir / f"{name}.generated.json", generated)
        render_docx(generated, layout, assets_dir, template, out_dir / f"{name}.docx", name)
        lines.append(generated["processing_log"][-1]["summary"])
    (out_dir / f"{name}.report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", type=Path, nargs="+", help="v010 protocol PDFs")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--llm", choices=["cli", "api"], default="cli")
    parser.add_argument("--model", help="model name for the chosen backend")
    parser.add_argument("--skip-generate", action="store_true", help="stop after the pre-LLM docx")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--crosswalk", type=Path, default=DEFAULT_CROSSWALK)
    args = parser.parse_args()
    backend = None if args.skip_generate else args.llm
    for path in args.inputs:
        if path.suffix.lower() != ".pdf":
            raise SystemExit(f"{path}: only .pdf input is supported so far")
        for line in convert_pdf(path, args.out_dir, args.template, args.crosswalk, backend, args.model):
            print(line)


if __name__ == "__main__":
    main()
