"""Write the document model to a .docx on the CPT template with python-docx, including the v011 title page."""

import copy
import io
import statistics

import docx
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.image.image import Image
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt

from converter.model import (
    DEFAULT_TABLE_FONT_SIZE, STYLE_NAMES, TWIPS_PER_PT, Figure, FrontHeading, Paragraph, Redaction, Subheading, Table,
)
from converter.title_fields import amendment_state, registry_rows


CELL_BULLET_INDENT_PT = 8.0  # the template bullet indent would leave narrow table columns no room for text
TABLE_PAGE_HEIGHT_SHARE = 0.84  # a one-page table may fill this much of the text height; the rest holds its caption and rows that wrap taller than the source
SCANNED_PAGE_VERTICAL_ALLOWANCE = 18.0  # pt for the image paragraph's spacing and baseline
TOC_DEPTH = 4

CPT_TITLE_LABELS = {
    "v011": {
        "confidentiality": "Sponsor Confidentiality Statement:",
        "title": "Full Title:", "short_title": "Short Title:", "acronym": "Trial Acronym:",
        "protocol_number": "Sponsor Protocol Identifier:", "amendment_number": "Amendment Identifier:",
        "version": "Version Number:", "date": "Version Date:", "compound": "Sponsor’s Investigational Product Code(s):",
        "phase": "Trial Phase:", "sponsor": "Sponsor Name and Address:", "sponsor_address": "Sponsor Name and Address:",
        "manufacturer": "Device Manufacturer Name and Address:",
        "product_names": "Investigational Product Name(s):", "co_sponsor": "Co-Sponsor Name and Address:",
        "local_sponsor": "Local Sponsor Name and Address:",
        "regulatory_ids": "Regulatory or Clinical Trial Identifier(s):", "signatory": "Sponsor Signatory:",
        "medical_monitor": "Medical Expert Contact:",
    },
}

ALIGNMENTS = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT}


class Writer:
    def __init__(self, template_path, version):
        self.doc = docx.Document(template_path)
        self.styles = STYLE_NAMES[version]
        self.body = self.doc.element.body
        self.heading_numpr = self._heading_numbering()
        self.bullet_num_id = self._style_num_id(self.styles["bullet"])
        self.portrait_sect, self.landscape_sect = self._section_templates()
        for child in list(self.body):
            if child.tag != qn("w:sectPr"):
                self.body.remove(child)
        self.final_sect = self.body.find(qn("w:sectPr"))
        self.landscape = False
        self.toc_anchor = None
        self.text_width_pt, self.text_height_pt = self._text_area_of(self.portrait_sect)

    def _heading_numbering(self):
        """numPr to attach to each heading level (v011 numbers headings on the paragraph, not the style)."""
        numbering = {}
        for paragraph in self.doc.paragraphs:
            name = paragraph.style.name
            if name in self.styles["headings"]:
                num_pr = paragraph._p.find(qn("w:pPr") + "/" + qn("w:numPr"))
                level = self.styles["headings"].index(name)
                if num_pr is not None and level not in numbering:
                    numbering[level] = num_pr
        if not numbering:
            return {}
        base = numbering.get(0)
        num_id = base.find(qn("w:numId")).get(qn("w:val"))
        return {level: level_num_pr(level, num_id) for level in range(len(self.styles["headings"]))}

    def _style_num_id(self, style_name):
        style = self.doc.styles[style_name].element
        num_id = style.find(qn("w:pPr") + "/" + qn("w:numPr") + "/" + qn("w:numId"))
        return num_id.get(qn("w:val")) if num_id is not None else None

    def _section_templates(self):
        portrait = landscape = None
        for sect in self.body.iter(qn("w:sectPr")):
            pg = sect.find(qn("w:pgSz"))
            is_landscape = pg is not None and pg.get(qn("w:orient")) == "landscape"
            if is_landscape and landscape is None:
                landscape = copy.deepcopy(sect)
            if not is_landscape and portrait is None:
                portrait = copy.deepcopy(sect)
        return portrait, landscape

    # -- low-level helpers

    def paragraph(self, style, spans=None, text=None):
        p = self.doc.add_paragraph(style=style)
        self.body.remove(p._p)
        self.final_sect.addprevious(p._p)
        if text is not None:
            p.add_run(text)
        for s in spans or []:
            self.add_run(p, s)
        return p

    @staticmethod
    def add_run(p, span, size=None):
        text = span.text.replace("\t", " ")
        if not text:
            return
        run = p.add_run(text)
        if span.bold:
            run.bold = True
        if span.italic:
            run.italic = True
        if span.superscript:
            run.font.superscript = True
        if size:
            run.font.size = Pt(size)

    def page_break(self):
        p = self.paragraph(self.styles["body"])
        p.add_run().add_break(docx.enum.text.WD_BREAK.PAGE)

    def set_orientation(self, landscape):
        if landscape == self.landscape or self.landscape_sect is None:
            return
        ending = copy.deepcopy(self.landscape_sect if self.landscape else self.portrait_sect)
        for tag in ("w:titlePg", "w:pgNumType"):  # the template's first section restarts numbering at 1
            for ref in ending.findall(qn(tag)):
                ending.remove(ref)
        p = self.paragraph(self.styles["body"])
        p._p.get_or_add_pPr().append(ending)
        self.landscape = landscape
        self.text_width_pt, self.text_height_pt = self._text_area_of(
            self.landscape_sect if landscape else self.portrait_sect)

    @staticmethod
    def _text_area_of(sect):
        """(width, height) in pt between the page margins."""
        pg = sect.find(qn("w:pgSz"))
        mar = sect.find(qn("w:pgMar"))
        width = int(pg.get(qn("w:w"))) - int(mar.get(qn("w:left"))) - int(mar.get(qn("w:right")))
        height = int(pg.get(qn("w:h"))) - int(mar.get(qn("w:top"))) - int(mar.get(qn("w:bottom")))
        return width / TWIPS_PER_PT, height / TWIPS_PER_PT

    def finish_sections(self):
        source = self.landscape_sect if self.landscape else self.portrait_sect
        if source is None:
            return
        for tag in ("w:pgSz", "w:pgMar"):
            old = self.final_sect.find(qn(tag))
            new = copy.deepcopy(source.find(qn(tag)))
            if old is not None and new is not None:
                old.addprevious(new)
                self.final_sect.remove(old)

    # -- content

    def numbered_heading(self, title, level):
        index = min(level, len(self.styles["headings"])) - 1
        p = self.paragraph(self.styles["headings"][index], text=title)
        if self.heading_numpr:
            p._p.get_or_add_pPr().insert(1, copy.deepcopy(self.heading_numpr[index]))

    def subheading(self, subheading):
        style = self.styles["front_heading"] if subheading.kind == "bold" else self.styles["field_label"]
        self.paragraph(style, text=subheading.title).paragraph_format.keep_with_next = True

    def body_paragraph(self, para):
        if para.kind == "bullet":
            p = self.paragraph(self.styles["bullet"], para.spans)
            if para.level and self.bullet_num_id:
                p._p.get_or_add_pPr().insert(1, level_num_pr(para.level, self.bullet_num_id))
            return p
        if para.kind == "caption":
            caption = self.paragraph(self.styles["caption"], para.spans)
            caption.alignment = ALIGNMENTS[para.align]
            caption.paragraph_format.keep_with_next = True
            return caption
        p = self.paragraph(self.styles["body"], para.spans)
        if para.indent:
            p.paragraph_format.left_indent = Pt(para.indent)
        return p

    def _new_table(self, n_rows, n_cols):
        """An empty Table Grid table placed at the end of the body, before the final section properties."""
        t = self.doc.add_table(rows=n_rows, cols=n_cols)
        self.body.remove(t._tbl)
        self.final_sect.addprevious(t._tbl)
        t.style = self.doc.styles["Table Grid"]
        return t

    def table(self, table):
        """Write a table sized like the source: its column widths, font sizes and cell padding,
        scaled down together when the table is wider than the page.

        A table that fit on one source page is kept on one page.
        """
        n_rows, n_cols = len(table.grid), len(table.col_widths)
        t = self._new_table(n_rows, n_cols)
        t.autofit = False
        keep_together = table.first_page == table.page
        scale = min(1.0, self.text_width_pt / max(sum(table.col_widths), 1))
        if keep_together:
            scale = min(scale, self.text_height_pt * TABLE_PAGE_HEIGHT_SHARE / max(table.height, 1))
        widths = [Pt(width * scale) for width in table.col_widths]
        for grid_col, width in zip(t._tbl.tblGrid.gridCol_lst, widths):
            grid_col.w = width
        for c, width in enumerate(widths):
            for cell in t.columns[c].cells:
                cell.width = width
        set_cell_margins(t, table.padding[0] * scale, table.padding[1] * scale)
        font_size = table_font_size(table) * scale
        for r, row in enumerate(table.grid):
            keep_with_next = keep_together and r < n_rows - 1
            for c, cell in enumerate(row):
                if cell is None:
                    continue
                self._write_cell(t, r, c, cell, font_size, scale, keep_with_next)
        for row in t.rows:
            row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        for row in t.rows[:header_row_count(table.grid)]:
            row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        if self.landscape:
            self.repeat_heading_above(t)

    def _write_cell(self, t, r, c, cell, font_size, scale, keep_with_next):
        n_cols = len(t.columns)
        target = t.cell(r, c)
        # A full-width cell that also spans rows leaves continuation rows with one full-width
        # vMerge cell, which the M11 validator's docx reader turns into a zero-cell row and crashes on
        rowspan = 1 if cell["colspan"] >= n_cols else cell["rowspan"]
        if rowspan > 1 or cell["colspan"] > 1:
            target = target.merge(t.cell(r + rowspan - 1, c + cell["colspan"] - 1))
        if cell["fill"]:
            shade_cell(target, cell["fill"])
        first = True
        for para in cell["paragraphs"]:
            p = target.paragraphs[0] if first else target.add_paragraph()
            first = False
            p.style = self.doc.styles[self.styles["bullet"] if para.kind == "bullet" else self.styles["table_text"]]
            p.alignment = ALIGNMENTS[getattr(para, "align", "left")]
            for s in para.spans:
                run_size = font_size if s.superscript or not s.size else s.size * scale
                self.add_run(p, s, size=run_size)
        if first:
            target.paragraphs[0].style = self.doc.styles[self.styles["table_text"]]
        for p, para in zip(target.paragraphs, cell["paragraphs"]):
            if getattr(para, "kind", "body") == "body" and getattr(para, "indent", 0):
                p.paragraph_format.left_indent = Pt(para.indent)
        for p in target.paragraphs:
            compact_cell_paragraph(p, font_size, keep_with_next)
            if p.style.name == self.styles["bullet"]:
                p.paragraph_format.left_indent = Pt(CELL_BULLET_INDENT_PT)
                p.paragraph_format.first_line_indent = Pt(-CELL_BULLET_INDENT_PT)

    def repeat_heading_above(self, table):
        """Make the heading and caption just before a landscape table its first header rows, so they repeat on every page the table spans."""
        titles = []
        previous = table._tbl.getprevious()
        while previous is not None and previous.tag == qn("w:p") and self._is_heading_or_caption(previous):
            titles.insert(0, previous)
            previous = previous.getprevious()
        for title in reversed(titles):
            self._move_into_header_row(table, title)

    def _move_into_header_row(self, table, paragraph):
        row = table.add_row()
        table._tbl.remove(row._tr)
        table._tbl.tblGrid.addnext(row._tr)
        cell = row.cells[0].merge(row.cells[-1])
        cell._tc.remove(cell.paragraphs[0]._p)
        cell._tc.append(paragraph)
        borders = OxmlElement("w:tcBorders")
        for side in ("top", "left", "bottom", "right"):
            border = OxmlElement(f"w:{side}")
            border.set(qn("w:val"), "nil")
            borders.append(border)
        cell._tc.get_or_add_tcPr().append(borders)
        properties = row._tr.get_or_add_trPr()
        properties.append(OxmlElement("w:cantSplit"))
        properties.append(OxmlElement("w:tblHeader"))

    def _is_heading_or_caption(self, paragraph):
        style = paragraph.find(qn("w:pPr") + "/" + qn("w:pStyle"))
        if style is None:
            return False
        names = [self.doc.styles[name].style_id for name in (*self.styles["headings"], self.styles["caption"])]
        return style.get(qn("w:val")) in names

    def simple_table(self, rows, header=True, fill=None, fill_columns=None):
        """A plain bordered table of text cells; the first row is the header unless `header` is False.

        `fill` shades the body cells (not the header) of `fill_columns`, or of every column when that is None.
        """
        t = self._new_table(len(rows), len(rows[0]))
        for row_index, (row, texts) in enumerate(zip(t.rows, rows)):
            for column, (cell, text) in enumerate(zip(row.cells, texts)):
                cell.paragraphs[0].style = self.doc.styles[self.styles["table_text"]]
                cell.paragraphs[0].add_run(text)
                is_body = row_index > 0 or not header
                if fill and is_body and (fill_columns is None or column in fill_columns):
                    shade_cell(cell, fill)
        if header:
            t.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))

    def figure(self, figure):
        width = min(figure.width_pt, self.text_width_pt)
        if figure.full_page:
            image = Image.from_blob(figure.png)
            available_height = self.text_height_pt - SCANNED_PAGE_VERTICAL_ALLOWANCE
            width = min(width, available_height * image.px_width / max(image.px_height, 1))
        p = self.paragraph(self.styles["body"])
        p.alignment = docx.enum.text.WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(io.BytesIO(figure.png), width=Pt(width))

    def toc(self):
        """Mark where the table of contents goes; fill_toc writes it once the headings are known."""
        self.paragraph(self.styles["field_label"], text="Table of Contents")
        self.toc_anchor = self.paragraph(self.styles["body"])

    def fill_toc(self, entries):
        """Write a TOC field whose cached result already lists the headings (without page numbers).

        Word shows the cached entries as-is; updating the field adds page numbers. This avoids
        w:updateFields, which makes Word prompt on every open.
        """
        entries = [e for e in entries if e[0] <= TOC_DEPTH] or [(1, "", "")]
        anchor = self.toc_anchor._p
        paragraphs = []
        for level, number, title in entries:
            p = self.doc.add_paragraph(style=f"toc {level}")
            self.body.remove(p._p)
            paragraphs.append(p)
            if number:
                p.add_run(f"{number}\t{title}")
            elif title:
                p.add_run(title)
        first, last = paragraphs[0], paragraphs[-1]
        start = first._p.makeelement(qn("w:r"), {})
        start.append(field_char("begin"))
        instr = OxmlElement("w:instrText")
        instr.set(qn("xml:space"), "preserve")
        instr.text = f' TOC \\o "1-{TOC_DEPTH}" \\h \\z \\u '
        start.append(instr)
        start.append(field_char("separate"))
        first._p.get_or_add_pPr().addnext(start)
        end = last._p.makeelement(qn("w:r"), {})
        end.append(field_char("end"))
        last._p.append(end)
        for p in reversed(paragraphs):
            anchor.addnext(p._p)
        anchor.getparent().remove(anchor)

    def footers(self, label):
        for section in self.doc.sections:
            for part in (section.footer, section.first_page_footer, section.even_page_footer,
                         section.header, section.first_page_header, section.even_page_header):
                if part.is_linked_to_previous:
                    continue
                for p in part.paragraphs:
                    clear_paragraph_text(p)
                for tbl in list(part._element.findall(qn("w:tbl"))):
                    part._element.remove(tbl)
                for control in list(part._element.findall(qn("w:sdt"))):  # the template's own page-number control
                    part._element.remove(control)
            footer = section.footer
            if not footer.is_linked_to_previous:
                p = footer.paragraphs[0]
                p.add_run(f"{label}\t")
                add_page_field(p)

    def save(self, path):
        self.doc.save(path)


def header_row_count(grid):
    """Leading rows whose cells are all shaded: the column headings, which repeat when the table spans pages.

    At most half the rows, so a table shaded throughout still has body rows to scroll under its headings.
    """
    count = 0
    for row in grid:
        cells = [cell for cell in row if cell]
        if not cells or not all(cell["fill"] for cell in cells):
            break
        count += 1
    return max(1, min(count, len(grid) // 2))


def table_font_size(table):
    sizes = [s.size for row in table.grid for cell in row if cell
             for para in cell["paragraphs"] for s in para.spans if s.size and not s.superscript]
    return statistics.median(sizes) if sizes else DEFAULT_TABLE_FONT_SIZE


def set_cell_margins(table, horizontal_pt, vertical_pt):
    margins = OxmlElement("w:tblCellMar")
    for side, size in (("top", vertical_pt), ("left", horizontal_pt),
                       ("bottom", vertical_pt), ("right", horizontal_pt)):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(round(size * TWIPS_PER_PT)))
        el.set(qn("w:type"), "dxa")
        margins.append(el)
    table._tbl.tblPr.append(margins)


def shade_cell(cell, hex_fill):
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    cell._tc.get_or_add_tcPr().append(shd)


def compact_cell_paragraph(p, font_size, keep_with_next):
    """Single-spaced with no paragraph spacing, so rows are as tall as the source's.

    Table Grid sets a 15 pt minimum line height that Table Text doesn't override.
    """
    fmt = p.paragraph_format
    fmt.space_before = Pt(0)
    fmt.space_after = Pt(0)
    fmt.line_spacing = 1.0
    fmt.keep_with_next = keep_with_next
    mark = OxmlElement("w:rPr")  # paragraph mark properties; last child of pPr
    p._p.get_or_add_pPr().append(mark)
    for tag in ("w:sz", "w:szCs"):
        el = OxmlElement(tag)
        el.set(qn("w:val"), str(round(font_size * 2)))
        mark.append(el)


def clear_paragraph_text(p):
    for child in list(p._p):
        if child.tag != qn("w:pPr"):
            p._p.remove(child)


def level_num_pr(level, num_id):
    num_pr = OxmlElement("w:numPr")
    ilvl = OxmlElement("w:ilvl")
    ilvl.set(qn("w:val"), str(level))
    num_id_element = OxmlElement("w:numId")
    num_id_element.set(qn("w:val"), num_id)
    num_pr.append(ilvl)
    num_pr.append(num_id_element)
    return num_pr


def field_char(kind):
    el = OxmlElement("w:fldChar")
    el.set(qn("w:fldCharType"), kind)
    return el


def add_page_field(p):
    run = p.add_run()
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    for element in (field_char("begin"), instruction, field_char("end")):
        run._r.append(element)


def write_block(writer, block):
    """Write one content block; blocks that carry no content (page marks, headings) are ignored."""
    if isinstance(block, Paragraph):
        writer.body_paragraph(block)
    elif isinstance(block, Table):
        writer.table(block)
    elif isinstance(block, Figure):
        writer.figure(block)
    elif isinstance(block, Redaction):
        writer.paragraph(writer.styles["body"], text="[Redacted: CCI]")
    elif isinstance(block, (FrontHeading, Subheading)):
        writer.subheading(block if isinstance(block, Subheading) else Subheading(block.title, "bold"))


V011_TITLE_ORDER = [
    "confidentiality", "title", "acronym", "protocol_number", "version", "date", "amendment_number", "compound",
    "product_names", "phase", "short_title", "sponsor", "co_sponsor", "local_sponsor", "manufacturer",
]

MISSING_FIELD_VALUE = "[Not found in source protocol]"
NOT_APPLICABLE = "Not applicable"

M11_IDENTIFIER_NAMES = {
    "IND": "FDA IND Number", "NCT": "NCT Number", "WHO": "WHO-UTN Number",
    "EU CT": "EU CT Number", "EU TRIAL NUMBER": "EU CT Number", "CTIS": "EU CT Number",
}

OTHER_IDENTIFIER_NAME = "Other Regulatory or Clinical Trial Identifier"

AMENDMENT_STATEMENTS = {
    "none": "This protocol has not been amended.",
    "first": "This is the first protocol amendment.",
    "later": "This protocol has been amended previously. Details of prior amendments are presented in Section 12.3 Prior Protocol Amendment(s).",
}

# Title-page fields M11 requires after the slot fields the template order covers
AFTER_PROTOCOL_NUMBER = ("Original Protocol:", "original_protocol")

AFTER_AMENDMENT_IDENTIFIER = ("Amendment Scope:", "amendment_scope")

# M11 technical specification, title page: Conformance "Optional", so the line is deleted when the source has no value
OPTIONAL_TITLE_KEYS = {
    "confidentiality", "acronym", "version", "date", "compound", "product_names", "short_title", "co_sponsor",
    "local_sponsor", "manufacturer", "signatory", "medical_monitor",
}


def write_title_page(writer, values, others, notes):
    """Write the v011 title page: the template's fields in its order, then fields it has no slot for."""
    styles = writer.styles
    labels = CPT_TITLE_LABELS["v011"]
    state = amendment_state(values)
    derived = {
        "original_protocol": "Yes" if state == "none" else "No",
        "amendment_scope": values.get("amendment_scope") or (NOT_APPLICABLE if state == "none" else MISSING_FIELD_VALUE),
    }
    # An original protocol has no Amendment Number line, but M11 still requires the Amendment Scope heading
    extra_after = {"protocol_number": [AFTER_PROTOCOL_NUMBER]}
    scope_anchor = "protocol_number" if state == "none" else "amendment_number"
    extra_after.setdefault(scope_anchor, []).append(AFTER_AMENDMENT_IDENTIFIER)

    def write_field(label, text):
        writer.paragraph(styles["field_label"], text=label)
        if text:
            writer.paragraph(styles["body"], text=text)

    writer.paragraph(styles["page_title"], text="Title Page")
    for key in V011_TITLE_ORDER:
        value = values.get(key)
        # Conformance "Conditional: when there is an amendment"
        if key == "amendment_number" and state == "none":
            continue
        if not value and key in OPTIONAL_TITLE_KEYS:
            continue
        if not value:
            notes.append(f"title page: {labels[key]} not found in source")
        # a code-list field can't hold the placeholder
        write_field(labels[key], value or ("" if key == "phase" else MISSING_FIELD_VALUE))
        for label, derived_key in extra_after.get(key, []):
            write_field(label, derived[derived_key])
    rows = [(M11_IDENTIFIER_NAMES.get(label, OTHER_IDENTIFIER_NAME), identifier)
            for label, identifier in registry_rows(values.get("regulatory_ids", ""))]
    write_field(labels["regulatory_ids"], "" if rows else MISSING_FIELD_VALUE)
    if rows:
        writer.simple_table(rows, header=False)
    else:
        notes.append("title page: regulatory identifiers not found in source")
    write_field("Sponsor Approval:", values.get("sponsor_approval") or values.get("date") or MISSING_FIELD_VALUE)
    for key in ("signatory", "medical_monitor"):
        if values.get(key):
            write_field(labels[key], values[key])
    write_field("Amendment Details", AMENDMENT_STATEMENTS[state])
    for label, value in others:
        if label:
            writer.paragraph(styles["field_label"], text=label)
        if value:
            writer.paragraph(styles["body"], text=value)
        notes.append(f"title page: extra field kept at the end: {label or value[:50]}")
    return values.get("protocol_number")
