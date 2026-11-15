import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import paths  # noqa: F401

from converter import cli

VALIDATOR_OUTPUT = "File:          a.docx\nFindings:      2 (errors=1, warnings=1, info=0)\nRESULT: INVALID\n"


class RunDocxValidatorsTests(unittest.TestCase):
    def test_summarises_result_and_saves_full_report(self):
        with tempfile.TemporaryDirectory() as folder:
            docx = Path(folder) / "a.docx"
            completed = CompletedProcess([], 1, stdout=VALIDATOR_OUTPUT, stderr="")
            with patch.object(cli.subprocess, "run", return_value=completed) as run:
                lines = cli.run_docx_validators(docx, {"m11": Path("tools/validate_m11.py")})
            self.assertEqual((Path(folder) / "a.m11.txt").read_text(encoding="utf-8"), VALIDATOR_OUTPUT)
        self.assertEqual(run.call_args.args[0][1:], ["tools/validate_m11.py", str(docx)])
        self.assertIn("RESULT: INVALID", lines[0])
        self.assertIn("errors=1", lines[0])
        self.assertIn("a.m11.txt", lines[1])

    def test_reports_stderr_when_validator_could_not_run(self):
        with tempfile.TemporaryDirectory() as folder:
            completed = CompletedProcess([], 2, stdout="", stderr="Not a .docx file")
            with patch.object(cli.subprocess, "run", return_value=completed):
                lines = cli.run_docx_validators(Path(folder) / "a.docx", {"cesharp": Path("v.py")})
        self.assertIn("Not a .docx file", lines[0])


if __name__ == "__main__":
    unittest.main()
