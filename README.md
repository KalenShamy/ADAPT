# Automated Document Assembly for Protocol Transformation (ADAPT)

Convert TransCelerate CPT v010 protocol PDFs or Word files into CPT v011 Word documents. Sections that v011 requires but v010 has no source for are drafted by an LLM from the protocol's own text and highlighted for review. Mapping uses `resources/research/crosswalk.tsv`.

## Install

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
```

The default LLM backend runs the local `codex` CLI, so it needs Codex installed and signed in. `--llm cli` runs the local `claude` CLI instead (Claude Code installed and signed in). For the API backend, also run `uv pip install anthropic` and set `ANTHROPIC_API_KEY`.

## Convert

```sh
cd src
../.venv/bin/python -m converter ../resources/example-protocols/v010/<name>.pdf --out-dir ../output
```

Options:

- `--llm cli|codex|api` picks the backend (default `codex`); `--model` overrides its model.
- `--skip-generate` stops after the pre-LLM document and makes no LLM calls.
- `--no-fhir` skips `<name>.fhir.json`, which is otherwise written from the generated JSON next to the docx (never written with `--skip-generate`).
- `--dump-context` writes `<name>.context.json`: each section's M11 format and the source content chosen for it.
- `--validate-m11` checks the final docx against the ICH M11 rules (`tools/validate_m11.py`, Python 3.12). The full report goes to `<name>.m11.txt` and a summary to `<name>.report.txt`.
- `--validate-cesharp` checks the final docx's CeSHarP structure by importing it to USDM and running the usdm4 rules (`tools/validate_cesharp.py`). The full report goes to `<name>.cesharp.txt`. With `--skip-generate` both validators check `<name>.pre-llm.docx`.
- `--template` and `--crosswalk` override the defaults.

Per input, in `--out-dir`:

| File | What it is |
|---|---|
| `<name>.json` | Protocol JSON (`schema/protocol.schema.json`), validated before anything else is written |
| `<name>.layout.json`, `<name>.assets/` | Layout sidecar: formatting, merged-cell tables, figures. Keep them with the JSON |
| `<name>.pre-llm.docx` | v011 document before any LLM content |
| `<name>.generated.json` | Protocol JSON after LLM drafting; drafted blocks have origin `ai_generated` |
| `<name>.docx` | v011 document with LLM-drafted sections highlighted yellow |
| `<name>.fhir.json` | Unless `--no-fhir`: FHIR document Bundle (see below) |
| `<name>.report.txt` | Unmatched sections and the LLM step's result |

Each draft prompt carries the ICH M11 specification's user guidance for that section (`resources/research/m11_user_guidance.json`) and, for a few rationale sections, a short style excerpt from a published v011 protocol. The LLM step only drafts sections the source gives a basis for. Sections it can't support stay red `[NOT EXTRACTED]`. The report lists which sections were drafted, which had no basis, and which failed. LLM calls then read structured fields from the protocol text: the 1.1.2 Overall Design values, the section 6 interventions table, the 9.2 safety event reporting table, 12.2 country requirements (only when 12.2 is empty), 12.3 amendment history and the section 3 objectives with their estimands. Each value is highlighted and flagged for review, and answers that don't fit the schema are dropped. Where objectives are extracted, the source text they replace is removed from section 3. Dose form and route stay as the source's own words because the schema wants EDQM codes the converter can't look up. In 5.2 and 5.3, numbered criteria are renumbered as one flat list and sponsor category subheadings are dropped (the report lists them). Sections the M11 specification marks optional or conditional that are still empty after drafting are left out of `<name>.docx` and listed in the report.

Input is a PDF or a Word (.docx) file. In a .docx, tracked insertions are kept and tracked deletions are dropped. In a PDF, scanned pages stay images; there is no OCR. In Word, update the table of contents to populate page numbers.

## FHIR export

`src/converter/export/fhir.py` turns protocol JSON into a FHIR `document` Bundle for the HL7 Clinical Study Protocol IG 1.0.0-ballot2 (FHIR 6.0.0-ballot3, package in `resources/reference/hl7-fhir/package.tgz`). It follows the IG's mapping sheet, `resources/M11 to FHIR Mapping 02.xlsx`:

- The title page and amendment details go to a `ResearchStudy` (M11 profile) and its M11 extensions, plus `Organization`, `Practitioner` and `MedicinalProductDefinition` resources.
- All section text goes into one `Composition`, with one section per heading, coded from the IG section value set. Formatting, tables and figures come from the layout sidecar; figures are embedded as `data:` images.
- Each section with AI-drafted content gets a `Provenance` that points at the section (`targetElement`) and names the model as a `Device`.
- The protocol JSON is taken as fact; its validity is checked upstream. Every value it holds is exported, including `unsure` slots and content flagged for review. The export step itself uses no AI.
- The exception is any slot or content block with `custody.confidence` of `low`, `very_low` or `none`. Its value is left out of the export and a visible `[Needs human input: …]` marker takes its place. A required element gets `data-absent-reason: temp-unknown` instead. A `Task` (status `requested`) names what to enter and points at the section, or at the ResearchStudy for title-page and amendment values. While any Task exists, the Composition is `preliminary` and the ResearchStudy `draft`; otherwise they are `final` and `active`.
- Values the profile requires but the JSON lacks carry `data-absent-reason`, and the report lists them. That is valid for a date, a string or an identifier value. It is not valid for a required code (the original-protocol indicator, the enrollment scope of an amendment's scope impact, the section of an amendment change): the profile's binding needs a code, so the bundle fails validation until the value is entered. The report marks these lines `NOT CONFORMANT`.

Identifier systems come from `resources/fhir/identifier-systems.json`; types set to null export without a system. To export an existing JSON:

```sh
cd src
../.venv/bin/python -m converter.export.fhir ../output/<name>.generated.json
```

To validate against the IG with the HL7 validator (Java 11+, [validator_cli.jar](https://github.com/hapifhir/org.hl7.fhir.core/releases)), run it offline from terminology servers:

```sh
java -jar validator_cli.jar output/<name>.fhir.json -version 6.0.0-ballot3 -ig resources/reference/hl7-fhir/package.tgz -tx n/a
```

Reading the result (validator 7.0.6, checked 2026-11-14):

- The validator has no 6.0.0-ballot3 core. It loads `hl7.fhir.r6.core#6.0.0-snapshot1`, a later R6 build, and says so in its `Loading FHIR` line. The IG and this export target ballot3, so the validator reports errors where the two builds differ: `Task.focus` (one Reference in ballot3, a repeating element in snapshot1), the Tasks then counting as unreachable from the Composition, and the `task-code` and `device-nametype` code systems (`http://hl7.org/fhir/...` in ballot3, `terminology.hl7.org` in snapshot1). These only appear when the bundle has a Task or a Device.
- "The extension ... is not allowed to be used at this point" on the ResearchStudy extensions also appears on the IG's own examples (`package/example/ResearchStudy-*.json`). It comes from how the IG declares the extension context.
- "No code provided, and a code is required" is a real gap in the data: see `NOT CONFORMANT` above.
- `-tx n/a` means no code is checked against a terminology server, so every NCIt code gets a "could not be validated" warning.

## Known issues

Each of these changes output when fixed, so they are listed here until then.

- Word input: a table cell that holds only "—" or "[REDACTED]" comes out empty.
- Trial phase on the title page is misread for some wordings, and still marked high confidence: "Phase 1b/2" becomes Phase 2, "Phase 2b/3" becomes Phase 3, and "A Phase 2 study of drug I" becomes Phase 2/Phase 1.
- Notes from reading the title page are not written to `<name>.report.txt`.
- PDF input: a wrapped line that starts with a short abbreviation ("vs. placebo", "etc. were", "no. 5") starts a new paragraph mid-sentence.
- PDF input: control characters in the PDF text (e.g. form feed) make the conversion crash. Not yet seen in a real PDF.
- `--llm api`: one API error stops the whole LLM step instead of failing only that section. `--llm codex` can mis-read replies containing non-ASCII characters (e.g. "≥") on systems whose default encoding isn't UTF-8.
- `--llm api` caps replies at 4096 tokens, which may cut off long objectives or analyses replies. Not checked against real protocols.
- If the model returns a single string where a list is expected (country requirements, objectives, analyses), the text is split into one-character paragraphs.
- `--crosswalk` is not used when building the LLM prompts' section context; those always use `resources/research/crosswalk.tsv`.
- Renumbering (`converter/render/renumber.py`) does not count objectives or added subsections, while the docx renderer does. A section with both objectives and numbered subsections would get wrong TOC and cross-reference numbers. No current section has both.
- The negative cases in `schema/test_protocol_schema.py` only check that validation fails, not which error it fails with, so a case can pass for the wrong reason.

## Test

```sh
.venv/bin/python -m unittest discover -s tests -v
cd schema && ../.venv/bin/python test_protocol_schema.py
```
