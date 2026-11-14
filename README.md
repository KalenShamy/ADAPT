# Protocol reformatter

Convert TransCelerate CPT v010 protocol PDFs into CPT v011 Word documents. Sections that v011 requires but v010 has no source for are drafted by an LLM from the protocol's own text and highlighted for review. Mapping uses `resources/research/crosswalk.tsv`.

## Install

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
```

The default LLM backend runs the local `claude` CLI, so it needs Claude Code installed and signed in. For the API backend, also run `uv pip install anthropic` and set `ANTHROPIC_API_KEY`.

## Convert

```sh
cd src
../.venv/bin/python -m converter ../resources/example-protocols/v010/<name>.pdf --out-dir ../output
```

Options:

- `--llm cli|api` picks the backend (default `cli`); `--model` overrides its model.
- `--skip-generate` stops after the pre-LLM document and makes no LLM calls.
- `--template` and `--crosswalk` override the defaults.

Per input, in `--out-dir`:

| File | What it is |
|---|---|
| `<name>.json` | Protocol JSON (`schema/protocol.schema.json`), validated before anything else is written |
| `<name>.layout.json`, `<name>.assets/` | Layout sidecar: formatting, merged-cell tables, figures. Keep them with the JSON |
| `<name>.pre-llm.docx` | v011 document before any LLM content |
| `<name>.generated.json` | Protocol JSON after LLM drafting; drafted blocks have origin `ai_generated` |
| `<name>.docx` | v011 document with LLM-drafted sections highlighted yellow |
| `<name>.report.txt` | Unmatched sections and the LLM step's result |

The LLM step only drafts sections the source gives a basis for. Sections it can't support stay red `[NOT EXTRACTED]`. The report lists which sections were drafted, which had no basis, and which failed.

Input must be a PDF. Scanned pages stay images; there is no OCR. In Word, update the table of contents to populate page numbers.

## Test

```sh
.venv/bin/python -m unittest discover -s tests -v
cd schema && ../.venv/bin/python test_protocol_schema.py
```
