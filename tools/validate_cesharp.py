"""Validate the CeSHarP (ICH M11) structured content of a protocol .docx.

Imports the docx into CDISC USDM v4 with usdm4_protocol's M11 importer, then runs the usdm4 rule
library (open source, data4knowledge) on the USDM JSON. Importer problems (content that could not
be mapped to a USDM element) are reported as findings too.
Needs Python 3.12 and the root requirements.txt.

Usage: python tools/validate_cesharp.py <file.docx> [--json out.json]

Exit codes are the same as tools/validate_m11.py: 0 valid, 1 error-level findings, 2 could not run,
3 incomplete (a rule crashed or is not implemented).
"""

import sys
import tempfile
from pathlib import Path

from validate_m11 import (
    FAILURE,
    SUCCESS,
    Finding,
    RuleResult,
    Validation,
    ValidatorFailed,
    rule_results,
    run_command_line,
    validate_in_utf8_process,
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
        return validate_in_utf8_process(Path(__file__), docx)

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
    rules += rule_results(results.outcomes, finding_from)
    return Validation(tuple(rules))


if __name__ == "__main__":
    sys.exit(run_command_line("Validate the CeSHarP (M11) structure of a protocol .docx", validate))
