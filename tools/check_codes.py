"""Check the C-codes in converted protocols against the NCI terminology.

Usage:
    python tools/check_codes.py out/*.docx
    python tools/check_codes.py out/*.json

Each docx is checked through the protocol JSON it was rendered from (see converter.audit.protocol_json_path),
since the docx only shows decodes. Every {code, decode} value in the JSON is checked:
  - the code looks like C<digits>
  - the code exists in the ICH M11 terminology (no `system`) or NCI Thesaurus (`system` other or CDISC SDTM)
  - the decode is the ICH preferred term or synonym of that code (or a synonym of the NCIt concept)
  - the same code is not used with different decodes
  - for a docx, the decode appears in the document text
Codes in systems with no local source (MedDRA, EDQM, WHO INN, ...) are counted but not checked.
Exits 1 if any check fails.
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import docx
from docx.oxml.ns import qn

from check_output import MAX_SHOWN
from converter.audit import protocol_json_path, squash
from converter.terminology import normalize
from converter.terminology.ncit import NCIT_FILE, SYNONYMS_COLUMN

M11_TERMINOLOGY = (Path(__file__).resolve().parent.parent / "resources" / "reference" / "nci-evs-m11"
                   / "ICH M11 Terminology.txt")
NCIT_SYSTEMS = {"other", "CDISC SDTM"}
M11_SYNONYM_SEPARATORS = ";|"
NCIT_LIST_SEPARATOR = "|"
C_CODE = re.compile(r"C\d+")


@dataclass(frozen=True)
class CodedValue:
    path: str
    system: str | None
    code: str
    decode: str


def find_coded_values(node: object, path: str = "") -> list[CodedValue]:
    found = []
    if isinstance(node, dict):
        code, decode = node.get("code"), node.get("decode")
        if isinstance(code, str) and isinstance(decode, str):
            found.append(CodedValue(path, node.get("system"), code, decode))
        for key, child in node.items():
            found += find_coded_values(child, f"{path}/{key}")
    elif isinstance(node, list):
        for index, child in enumerate(node):
            found += find_coded_values(child, f"{path}[{index}]")
    return found


def load_m11_terms() -> dict[str, tuple[set[str], str]]:
    """code -> (accepted names, codelist name) for every term and codelist in the M11 terminology."""
    terms = {}
    with M11_TERMINOLOGY.open(encoding="utf-8", newline="") as lines:
        for row in csv.DictReader(lines, delimiter="\t"):
            names = {row["ICH Preferred Term"], row["NCI Preferred Term"]}
            synonyms = row["ICH Synonym(s)"]
            for separator in M11_SYNONYM_SEPARATORS:
                synonyms = synonyms.replace(separator, "\n")
            names.update(synonyms.splitlines())
            terms[row["Code"]] = ({normalize(name) for name in names if name}, row["Codelist Name"])
    return terms


def load_ncit_names(codes: set[str]) -> dict[str, set[str]]:
    """code -> normalized names, read in one pass over the flat file for just the codes asked for."""
    names = {}
    if not codes or not NCIT_FILE.exists():
        return names
    with NCIT_FILE.open(encoding="utf-8") as lines:
        for line in lines:
            code, _, rest = line.partition("\t")
            if code in codes:
                row = line.rstrip("\n").split("\t")
                names[code] = {normalize(name) for name in row[SYNONYMS_COLUMN].split(NCIT_LIST_SEPARATOR)}
    return names


def check_values(values: list[CodedValue], m11_terms, ncit_names) -> tuple[list[str], int]:
    problems, unchecked = [], 0
    decodes_by_code = defaultdict(set)
    for value in values:
        decodes_by_code[value.code].add(value.decode)
        where = f"{value.path}: {value.code} {value.decode!r}"
        if not C_CODE.fullmatch(value.code):
            problems.append(f"{where}: not a C-code")
        elif value.system is None:
            problems += check_m11_value(value, where, m11_terms)
        elif value.system in NCIT_SYSTEMS:
            problems += check_ncit_value(value, where, ncit_names)
        else:
            unchecked += 1
    for code, decodes in decodes_by_code.items():
        if len(decodes) > 1:
            problems.append(f"{code} used with different decodes: {sorted(decodes)}")
    return problems, unchecked


def check_m11_value(value: CodedValue, where: str, m11_terms) -> list[str]:
    if value.code not in m11_terms:
        return [f"{where}: code is not in the ICH M11 terminology"]
    names, codelist = m11_terms[value.code]
    if normalize(value.decode) not in names:
        return [f"{where}: decode does not match the ICH term in '{codelist}'"]
    return []


def check_ncit_value(value: CodedValue, where: str, ncit_names) -> list[str]:
    if not ncit_names:
        return []
    if value.code not in ncit_names:
        return [f"{where}: code is not in the NCI Thesaurus"]
    if normalize(value.decode) not in ncit_names[value.code]:
        return [f"{where}: decode is not a name of that NCI Thesaurus concept"]
    return []


def check_in_document(values: list[CodedValue], document) -> list[str]:
    body_text = squash("".join(text.text or "" for text in document.element.body.iter(qn("w:t"))))
    missing = sorted({value.decode for value in values if squash(value.decode) not in body_text})
    return [f"{len(missing)} decodes not in the docx text, e.g. {missing[:MAX_SHOWN]}"] if missing else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("outputs", nargs="+", type=Path, help="converted .docx files or protocol .json files")
    args = parser.parse_args()
    m11_terms = load_m11_terms()
    failed = False
    for path in args.outputs:
        document = docx.Document(str(path)) if path.suffix == ".docx" else None
        json_path = protocol_json_path(path) if document else path
        if not json_path.exists():
            print(f"FAIL  {path.name}\n      missing protocol JSON {json_path.name}")
            failed = True
            continue
        values = find_coded_values(json.loads(json_path.read_text(encoding="utf-8")))
        ncit_codes = {value.code for value in values if value.system in NCIT_SYSTEMS}
        problems, unchecked = check_values(values, m11_terms, load_ncit_names(ncit_codes))
        if document:
            problems += check_in_document(values, document)
        failed |= bool(problems)
        print(f"{'FAIL' if problems else 'ok'}  {path.name}  ({len(values)} codes, {unchecked} in systems with no local source)")
        for problem in problems:
            print(f"      {problem}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
