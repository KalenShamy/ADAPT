"""Validate the CeSHarP (ICH M11) structured content of a protocol .docx.

Imports the docx into CDISC USDM v4 with usdm4_protocol's M11 importer, then runs the usdm4 rule
library (open source, data4knowledge) on the USDM JSON. Importer problems (content that could not
be mapped to a USDM element) are reported as findings too.
Needs Python 3.12 and the root requirements.txt.

Usage: python tools/validate_cesharp.py <file.docx> [--json out.json]

Exit codes are the same as tools/validate_m11.py: 0 valid, 1 error-level findings, 2 could not run,
3 incomplete (a rule crashed or is not implemented).
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from validate_m11 import (
    EXIT_CODES,
    EXIT_NOT_RUN,
    FAILURE,
    SUCCESS,
    Finding,
    RuleResult,
    Validation,
    ValidatorFailed,
    format_report,
)

IMPORT_RULE_ID = "CESHARP_IMPORT"
ERROR_LEVELS = {"error", "exception", "critical"}


def severity_of(level: str) -> str:
    level = level.lower()
    return "error" if level in ERROR_LEVELS else level


def finding_from(row: dict) -> Finding:
    location = row.get("location") or {}
    element = ".".join(part for part in (location.get("klass"), location.get("attribute")) if part)
    return Finding(
        severity=severity_of(row["level"]),
        section=(location.get("path") or location.get("section") or "").strip(),
        element=element or (location.get("element") or "").strip(),
        message=row["message"],
    )


def validate(docx: Path) -> Validation:
    """Import docx to USDM, then run the usdm4 rule library on the result."""
    if not sys.flags.utf8_mode:
        return validate_in_utf8_process(docx)

    from simple_error_log.errors import Errors
    from usdm4 import USDM4
    from usdm4_protocol import USDM4M11

    with tempfile.TemporaryDirectory() as work_dir:
        importer = USDM4M11()
        wrapper = importer.from_docx(str(docx), work_dir=work_dir)
        if wrapper is None:
            raise ValidatorFailed(f"The M11 importer could not read {docx.name}.\n{importer.errors.dump().strip()}")
        usdm_path = Path(work_dir) / "usdm.json"
        usdm_path.write_text(wrapper.model_dump_json(), encoding="utf-8")
        results = USDM4().validate(str(usdm_path))

    import_findings = tuple(finding_from(row) for row in importer.errors.to_dict(level=Errors.DEBUG))
    rules = [RuleResult(IMPORT_RULE_ID, FAILURE if import_findings else SUCCESS, import_findings)]
    for rule_id, outcome in sorted(results.outcomes.items()):
        findings = tuple(finding_from(row) for row in outcome.errors.to_dict(level=Errors.DEBUG))
        reason = (outcome.exception or "").strip().splitlines()
        rules.append(RuleResult(rule_id, outcome.status.value, findings, reason[0] if reason else ""))
    return Validation(tuple(rules))


def validate_in_utf8_process(docx: Path) -> Validation:
    with tempfile.TemporaryDirectory() as out_dir:
        json_path = Path(out_dir) / "validation.json"
        command = [sys.executable, "-X", "utf8", __file__, str(docx), "--json", str(json_path)]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        if not json_path.is_file():
            raise ValidatorFailed(completed.stderr.strip() or f"validator exited with code {completed.returncode}")
        return Validation.from_json(json_path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the CeSHarP (M11) structure of a protocol .docx")
    parser.add_argument("file", type=Path)
    parser.add_argument("--json", type=Path, help="also write the per-rule results to this JSON file")
    args = parser.parse_args()

    if args.file.suffix.lower() != ".docx" or not args.file.is_file():
        print(f"Not a .docx file: {args.file}", file=sys.stderr)
        return EXIT_NOT_RUN
    try:
        validation = validate(args.file)
    except ValidatorFailed as error:
        print(error, file=sys.stderr)
        return EXIT_NOT_RUN

    print(format_report(args.file.name, validation))
    if args.json:
        args.json.write_text(validation.to_json(), encoding="utf-8")
    return EXIT_CODES[validation.verdict]


if __name__ == "__main__":
    sys.exit(main())
