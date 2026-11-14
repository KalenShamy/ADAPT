# Protocol reformatter

Convert TransCelerate CPT v010 protocol PDFs into CPT v011 Word documents through a portable USDM v4 JSON hand-off. Mapping uses `resources/research/crosswalk.tsv`; the converter makes no LLM calls.

## Install

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python -r requirements.txt
```

## Convert

```sh
.venv/bin/python src/extract_to_usdm.py --out-dir output resources/example-protocols/v010/*.pdf
.venv/bin/python src/write_v011_docx.py --out-dir output output/*.usdm.json
```

Extraction writes `<name>.usdm.json`, `<name>.usdm.assets/` when images exist, and `<name>.usdm.validation.txt`. Keep the JSON and its asset directory together. The writer needs no PDF and can write into a different directory. Use `--template` or `--crosswalk` to override its defaults.

Writing produces `<name>.docx`, `<name>.report.txt`, and `<name>.audit.json`. Reports list missing title fields, unmapped sections, splits requiring review, narrative changes, source bookmark differences, and USDM rule findings. Scanned pages remain images; there is no OCR. Unknown structured design and population data are omitted rather than inferred from prose.

The JSON uses USDM models with a document-version layout extension and loss-free XHTML `data-*` attributes. These attributes are outside the restricted USDM-XHTML schema, so rule DDF00187 fails. Other rule failures are reported separately. This is a layout-preserving interchange format, not a claim of fully conformant USDM-XHTML.

## Check

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python src/check_output.py output/*_Protocol.docx
```

The checker verifies the template outline, styles, reference targets, and the exact text/table/image count of every written narrative block. It also checks table provenance and reports source bookmark differences. It exits nonzero for findings and appends them to each report; findings are not waived merely because a report exists.

Extraction checks exact block equality after every XHTML round trip. USDM rule validation runs at extraction and again when writing. Review unresolved references and reported synopsis omissions before using a document.

In Word, update the table of contents to populate page numbers. Visual comparison and TOC updating remain manual checks, particularly for `2025-523252-31-00` and `2025-524581-14-00`.
