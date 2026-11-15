"""Render protocol JSON (schema/protocol.schema.json) to a v011 docx on the CPT template."""

import re
from pathlib import Path

from docx.enum.text import WD_COLOR_INDEX
from docx.oxml.ns import qn
from docx.shared import RGBColor

from block_xhtml import xhtml_to_blocks
from converter.parts import COLLECTION_PARTS, COLLECTION_SECTION_PATH
from converter.place import SOURCE_TABLE_CAPTION, captioned_tables
from converter.title_page import TITLE_PAGE_UNMAPPED_ID, render_values
from converter.render.renumber import (
    displayed_numbers, is_sub_label, ordered_children, renumber_blocks, renumber_text,
)
from pdf_to_cpt_docx import (
    MISSING_FIELD_VALUE, Figure, PageMark, Table, Writer, advance, amendment_state, write_block, write_title_page,
)

type JsonObject = dict[str, object]

TEMPLATE_VERSION = "v011"
MISSING_TEXT = "[NOT EXTRACTED: no content found in the source protocol]"
NOT_APPLICABLE_TEXT = "Not applicable"
LOW_CONFIDENCE_NOTE = "[AI-drafted from the source protocol. Review required.]"  # only on output with no source basis
LOW_CONFIDENCE_TEXT_COLOR = RGBColor(0xFF, 0xFF, 0xFF)
LOW_CONFIDENCE = "very_low"
NO_BASIS_NOTE = "[AI draft failed; human authorship required]"
NO_BASIS_CONFIDENCE = "none"
AI_TABLE_FILL = "FFFF00"  # Word's yellow highlight, so generated table cells match highlighted generated text
VALUE_COLUMN = (1,)  # label/value tables: column 0 holds the fixed M11 labels
AI_ORIGINS = ("ai_generated", "ai_transformed")
BRACKET_COLOR = RGBColor(0xC0, 0x00, 0x00)
BRACKET_SPAN = re.compile(r"(\[[^\]]*\])")
UNSPLIT_TEXT = "[Not separated in the source protocol; see the section text below]"

# Fixed M11 wording the validator checks; the source's own lead-in sentence is replaced, not repeated
LEAD_INS = {
    "5.2": "To be eligible to participate in this trial, an individual must meet all the following criteria:",
    "5.3": "An individual who meets any of the following criteria will be excluded from participation in this trial:",
    "11.1": "This trial will be conducted in accordance with the protocol and with the following:",
}
CLOSING_TEXT = {
    "5.1": "Prospective approval of protocol deviations to recruitment and enrollment criteria, "
           "also known as protocol waivers or exemptions, is not permitted.",
}
LABEL_PARAGRAPHS = {
    "1.1.2": [
        "Intervention:", "Intervention Model:", "Control Type:", "Control Description:",
        "Intervention Assignment Method:", "Stratification:", "Population Type:",
        "Population Diagnosis or Condition:", "Population Age:", "Minimum:", "Maximum:",
        "Site Distribution and Geographic Scope:", "Master Protocol:",
        "Drug/Device Combination Product Indicator:", "Adaptive Trial Design:", "Number of Arms:",
        "Trial Blind Schema:",
        "Blinded Roles: The following roles indicated will not be made aware of the treatment group assignment during the trial:",
        "Number of Participants:", "Duration:",
        "Total planned duration of trial intervention for each participant:",
        "Total planned duration of trial participation for each participant:",
        "Committees:", "Independent Committees:", "Other Committees:",
    ],
}
# 1.1.2 label -> the slots whose filled values are written after it
LABEL_SLOTS = {
    "Intervention:": ["intervention_codes", "intervention_nonproprietary_names"],
    "Population Diagnosis or Condition:": ["population_diagnosis_or_condition"],
    "Intervention Model:": ["intervention_model"],
    "Control Type:": ["control_type"],
    "Control Description:": ["control_description"],
    "Intervention Assignment Method:": ["intervention_assignment_method", "randomisation_type", "other_assignment_method"],
    "Stratification:": ["stratification"],
    "Population Type:": ["population_type"],
    "Minimum:": ["population_age.minimum", "population_age.minimum_unit"],
    "Maximum:": ["population_age.maximum", "population_age.maximum_unit"],
    "Site Distribution and Geographic Scope:": ["site_distribution", "site_geographic_scope"],
    "Master Protocol:": ["master_protocol"],
    "Drug/Device Combination Product Indicator:": ["drug_device_combination_product"],
    "Adaptive Trial Design:": ["adaptive_trial_design"],
    "Number of Arms:": ["number_of_arms"],
    "Trial Blind Schema:": ["trial_blind_schema"],
    "Blinded Roles: The following roles indicated will not be made aware of the treatment group assignment during the trial:": ["blinded_roles"],
    "Independent Committees:": ["independent_committees"],
    "Other Committees:": ["other_committees"],
    "Number of Participants:": ["participant_number_qualifier", "number_of_participants", "participant_number_basis"],
    "Total planned duration of trial intervention for each participant:": ["intervention_duration", "intervention_duration_description"],
    "Total planned duration of trial participation for each participant:": ["participation_duration", "participation_duration_description"],
}
PARTICIPANTS_LABEL = "Number of Participants:"
LINE_PER_SLOT_LABELS = {"Intervention:"}  # codes and nonproprietary names go on separate lines
WIDE_TABLE_COLUMNS = 8  # tables this wide are too cramped in portrait
FIELD_TABLES = [
    ("6", "interventions", "Overview of Trial Interventions", [
        ("Arm Name", "arm_name"), ("Arm Type", "arm_type"), ("Intervention Name", "intervention_name"),
        ("Intervention Type", "intervention_type"), ("Pharmaceutical Dose Form", "dose_form"),
        ("Dosage Strength(s)", "dosage_strengths"), ("Dosage Level(s)", "dosage_levels"),
        ("Route of Administration", "route"), ("Regimen/Treatment Period/Vaccination Regimen", "regimen"), ("Use", "use"),
        ("IMP/NIMP", "imp_or_nimp"), ("Sourcing", "sourcing")]),
    ("9.2", "safety_event_reporting", "Timing and Methods for Reporting Safety Events", [
        ("Event Type", "event_type"), ("Situational Scope", "situational_scope"),
        ("Reportable Period Start", "reportable_period_start"), ("Reportable Period End", "reportable_period_end"),
        ("Timing for Reporting to Sponsor or Designee", "timing_for_reporting"), ("Method for Reporting", "reporting_method"),
        ("Backup Method for Reporting", "backup_reporting_method")]),
    ("1.1.1", "summary_rows", "Primary and Secondary Objectives and Estimands", [("Objectives", "objective"), ("Estimand Summary", "estimand_summary")]),
]
PRIOR_AMENDMENTS_SECTION = "12.3"
PRIOR_AMENDMENT_STATEMENTS = {
    "none": "This protocol has not been amended.",
    "first": "This is the first protocol amendment.",
    "later": "This protocol has been amended previously. The Protocol Amendment Summary of Changes for the current "
             "amendment is located directly before the Table of Contents. Prior amendment(s) to this protocol are "
             "listed in the table below, beginning with the most recent.",
}
PRIOR_AMENDMENT_COLUMNS = ["Document", "Sponsor Approval Date", "Approximate Enrollment when Sponsor Approved Amendment"]
ORIGINAL_PROTOCOL_ROW = "Original Protocol"
ORIGINAL_PROTOCOL_ENROLLMENT = "0"
OVERVIEW_BELOW = "The Overview of Changes from each prior protocol amendment is provided below."
AMENDMENT_WORD = re.compile(r"^amendment\s+", re.IGNORECASE)
ESTIMAND_COLUMNS = ["population", "treatment", "endpoint", "population_level_summary"]
ESTIMAND_HEADINGS = ["Population", "Treatment", "Endpoint", "Population-level Summary"]
OPTIONAL_LABELS = {"Other Committees:"}  # M11 Optional: written only when the source states them
HEADING_LABELS = {"Population Age:", "Duration:", "Committees:"}  # M11 headings that only group the labels after them
OVERALL_DESIGN_INTRO = "Key aspects of the trial design are summarised below."
SAFETY_IMPACT_QUESTION = "Is this amendment likely to have a substantial impact on the safety or rights of the participants?"
DATA_IMPACT_QUESTION = ("Is this amendment likely to have a substantial impact on the reliability and robustness of "
                        "the data generated in the clinical trial?")
CHANGE_COLUMNS = ("Description of Change", "Brief Rationale for Change", "Section # and Name")
COLLECTION_SECTION_NUMBER = COLLECTION_SECTION_PATH[-1]
UNPLACED_APPENDIX_PARENT = "12"
UNPLACED_APPENDIX_TITLE = "Source Protocol Front Matter"
TABLE_REFERENCE = re.compile(r"\bTable\s+(\d+(?:[.\-]\d+)*)\b")
CURRENT_AMENDMENT_TITLE = "Current Amendment"
CURRENT_CHANGES_TITLE = "Overview of Changes in the Current Amendment"
PRIOR_AMENDMENTS_TITLE = "Prior Protocol Amendments"
ESTIMAND_TITLE = "Estimand Characteristics"


class DocxRenderer:
    def __init__(self, template: Path, layout: dict[str, str], assets_dir: Path, protocol_label: str) -> None:
        self.writer = Writer(str(template), TEMPLATE_VERSION)
        self.layout = layout
        self.assets_dir = assets_dir
        self.protocol_label = protocol_label
        self.toc_entries: list[tuple[int, str, str]] = []
        self.protocol_number = ""
        self.shifted_numbers: dict[str, str] = {}
        self.heading_counters: list[int] = []
        self.unplaced: list[JsonObject] = []
        self.amendment = "none"
        self.table_count = 0
        self.source_captions: dict[int, JsonObject] = {}  # id(table block) -> the source caption paragraph above it
        self.caption_ids: set[int] = set()
        self.table_numbers: dict[str, int] = {}  # source table label -> number shown in this document
        self.heading_title = ""

    def render(self, protocol: JsonObject, out_path: Path) -> None:
        writer = self.writer
        sections = protocol["sections"]
        self.shifted_numbers = displayed_numbers(sections)
        self.unplaced = protocol.get("unplaced_v10_content", [])
        self.source_captions = {id(table): caption for caption, table in captioned_tables(protocol)}
        self.caption_ids = {id(caption) for caption in self.source_captions.values()}
        self.amendment = self.title_page(protocol)
        if self.amendment != "none":
            self.current_amendment(protocol["amendment_details"])
        writer.page_break()
        writer.toc()
        writer.page_break()
        for key in sorted(sections, key=int):
            self.section(sections[key], level=1)
        writer.finish_sections()
        writer.fill_toc(self.toc_entries)
        writer.footers(self.protocol_label or self.protocol_number)
        self.renumber_table_references()
        writer.save(str(out_path))

    def title_page(self, protocol: JsonObject) -> str:
        """Write the title page and the Amendment Details statement; return the amendment state."""
        values = render_values(protocol["title_page"])
        self.protocol_number = write_title_page(self.writer, values, [], []) or ""
        return amendment_state(values)

    def current_amendment(self, details: JsonObject) -> None:
        """The M11 Current Amendment table and Overview of Changes, from the extracted amendment details."""
        styles = self.writer.styles
        self.writer.paragraph(styles["field_label"], text="Current Amendment")
        self.writer.paragraph(styles["body"], text="The table below describes the current amendment.")
        if is_low_confidence(details):
            self.low_confidence_note()
        enrolled = details["approximate_enrolled"]
        unit = enrolled["unit"].get("value")
        self.table_caption(CURRENT_AMENDMENT_TITLE)
        self.writer.simple_table([
            [f"Approximate {'%' if unit == 'percent' else '#'} Enrolled at Time of Sponsor Approval:",
             enrollment_text(enrolled, details["enrollment_scope"])],
            ["Reason(s) for Amendment:", reasons_text(details)],
            ["Amendment Summary:", cell_text(details["summary"]) or MISSING_FIELD_VALUE],
            [SAFETY_IMPACT_QUESTION, impact_text(details["substantial_impact_on_safety_or_rights"],
                                                 details["safety_or_rights_explanation"])],
            [DATA_IMPACT_QUESTION, impact_text(details["substantial_impact_on_data_reliability"],
                                               details["data_reliability_explanation"])],
        ], header=False, fill=AI_TABLE_FILL, fill_columns=VALUE_COLUMN)
        self.writer.paragraph(styles["field_label"], text=CURRENT_CHANGES_TITLE)
        self.changes_table(details["changes"], CURRENT_CHANGES_TITLE)

    def changes_table(self, changes: list[JsonObject], title: str) -> None:
        rows = [[cell_text(change["description"]), cell_text(change["rationale"]), self.section_name(change["section"])]
                for change in changes]
        rows = [row for row in rows if any(row)] or [[MISSING_FIELD_VALUE, "", ""]]
        self.table_caption(title)
        self.writer.simple_table([list(CHANGE_COLUMNS), *rows], fill=AI_TABLE_FILL)

    def section_name(self, slot: JsonObject) -> str:
        """'5.3 Exclusion Criteria' with the number Word shows once omitted sections are dropped."""
        text = cell_text(slot)
        number, _, name = text.partition(" ")
        if number in self.shifted_numbers:
            return f"{self.shifted_numbers[number]} {name}"
        return text

    def section(self, section: JsonObject, level: int) -> None:
        status = section["status"]
        sub_label = is_sub_label(section)
        # an unnumbered sub-label with nothing under it is left out rather than written over a red marker
        if status == "omitted" or (sub_label and status == "missing" and not is_no_basis(section.get("custody"))):
            return
        number = section.get("number") or ""
        content = section.get("content", [])
        landscape = self.landscape_blocks(content)
        self.start_with_orientation_of(content, landscape)
        if sub_label:
            self.writer.paragraph(self.writer.styles["field_label"], text=str(section["title"]))
        else:
            self.heading(str(section["title"]), level)
        if number == "1.1.1" and section.get("fields", {}).get("summary_rows"):
            content = []  # the rows table replaces the source's synopsis table
        content = self.fixed_text(number, content, section.get("fields", {}))
        # 3.2, 3.3 and 10.5 stay not_applicable until their items are generated, even with source text held under them
        holds_source_text = status == "not_applicable" and bool(section.get("additional_subsections"))
        if number != PRIOR_AMENDMENTS_SECTION and not holds_source_text:  # 12.3's M11 statement always fills it
            self.status_text(status, content, section.get("custody"))
        children = ordered_children(section)
        if number == COLLECTION_SECTION_NUMBER:
            self.collection_parts(content, children, landscape)
            children = []
        else:
            for block in content:
                self.block(block, landscape[id(block)])
        if number in CLOSING_TEXT:
            self.writer.paragraph(self.writer.styles["body"], text=CLOSING_TEXT[number])
        for criterion in section.get("fields", {}).get("criteria", []):
            self.criterion(criterion)
        self.field_tables(number, section.get("fields", {}))
        self.objectives(section.get("objectives", []), level + 1)
        for child in children:
            self.section(child, level + 1)
        for extra in section.get("additional_subsections", []):
            self.additional(extra, level + 1)
        if number == UNPLACED_APPENDIX_PARENT:
            self.unplaced_appendix(level + 1)

    def collection_parts(self, content: list[JsonObject], children: list[JsonObject],
                         landscape: dict[int, bool]) -> None:
        """9.2.2: untagged text first, then each M11 element as an unnumbered heading over its blocks.

        Recording and Follow-up are also schema subsections; their content joins the blocks tagged with that part.
        """
        child_content = {str(child["title"]): child.get("content", []) for child in children}
        unsorted = [block for block in content if not block.get("part")]
        placeholder_text_below = bool(content) and len(unsorted) == len(content)
        if not placeholder_text_below:
            for block in unsorted:
                self.block(block, landscape[id(block)])
        for part in COLLECTION_PARTS:
            self.writer.paragraph(self.writer.styles["field_label"], text=part)
            blocks = [block for block in content if block.get("part") == part]
            for block in blocks:
                self.block(block, landscape[id(block)])
            extra = child_content.get(part, [])
            for block in extra:
                self.block(block)
            if blocks or extra:
                continue
            if placeholder_text_below:
                self.placeholder(UNSPLIT_TEXT)
            else:
                self.missing_text()
        if placeholder_text_below:
            for block in unsorted:
                self.block(block, landscape[id(block)])

    def unplaced_appendix(self, level: int) -> None:
        """Source front matter with no M11 slot, kept as an additional appendix (M11 12.X) so nothing is lost."""
        if not self.unplaced:
            return
        self.heading(UNPLACED_APPENDIX_TITLE, level)
        for item in self.unplaced:
            heading = item["v10_source"]["found_heading"]
            if is_title_field(item):
                if heading:
                    self.writer.paragraph(self.writer.styles["field_label"], text=heading)
                for block in item["blocks"]:
                    self.block(block)
                continue
            if heading:
                self.heading(heading, level + 1)
            for block in item["blocks"]:
                self.block(block)

    def fixed_text(self, number: str, content: list[JsonObject], fields: JsonObject) -> list[JsonObject]:
        """Write the M11 text fixed for this section; return the source blocks still to write."""
        body = self.writer.styles["body"]
        if number in LEAD_INS:
            self.writer.paragraph(body, text=LEAD_INS[number])
            return content[1:] if content and is_lead_in(content[0]) else content
        if number == PRIOR_AMENDMENTS_SECTION:
            # M11: with no prior amendment, the statement is the whole section
            return content if self.prior_amendments(fields) else []
        if number == "1.1.2":
            self.writer.paragraph(body, text=OVERALL_DESIGN_INTRO)
        for label in LABEL_PARAGRAPHS.get(number, []):
            filled = label_text(label, fields)
            if label in OPTIONAL_LABELS and not filled:
                continue
            self.writer.paragraph(self.writer.styles["field_label"], text=label)
            if filled and is_low_confidence(label_slots(label, fields)):
                self.low_confidence_note()
            if filled:
                self.highlighted(filled)
            elif label not in HEADING_LABELS:
                self.placeholder(UNSPLIT_TEXT, body)
        return content

    def field_tables(self, number: str, fields: JsonObject) -> None:
        """Tables M11 builds from record fields: interventions (6), safety event reporting (9.2), amendments (12.3)."""
        for table_number, key, title, columns in FIELD_TABLES:
            records = [record for record in fields.get(key, []) if any(cell_text(record[name]) for _, name in columns)]
            rows = [[cell_text(record[name]) for _, name in columns] for record in records]
            if table_number == number and rows:
                wide = len(columns) >= WIDE_TABLE_COLUMNS
                self.writer.set_orientation(wide)
                if is_low_confidence(records):
                    self.low_confidence_note()
                self.table_caption(title)
                self.writer.simple_table([[heading for heading, _ in columns], *rows], fill=AI_TABLE_FILL)
                self.writer.set_orientation(False)
        for region in fields.get("country_regions", []):
            identifier = region["identifier"]["value"]
            self.writer.paragraph(self.writer.styles["field_label"], text=f"{identifier['decode']} ({identifier['code']})")
            if is_low_confidence(region):
                self.low_confidence_note()
            for block in [*region["requirements"], *region["clarifications"]]:
                self.block(block)

    def prior_amendments(self, fields: JsonObject) -> bool:
        """12.3: the M11 statement, then for a later amendment the prior-amendment table and their changes.

        Return True when there are prior amendments.
        """
        prior = [record for record in fields.get("prior_amendments", []) if has_filled_slot(record)]
        state = "later" if prior else self.amendment
        self.writer.paragraph(self.writer.styles["body"], text=PRIOR_AMENDMENT_STATEMENTS[state])
        if state != "later":
            return False
        if is_low_confidence(prior):
            self.low_confidence_note()
        prior.sort(key=amendment_order, reverse=True)
        rows = [[cell_text(record["amendment_identifier"]) or MISSING_FIELD_VALUE,
                 cell_text(record["sponsor_approval_date"]) or MISSING_FIELD_VALUE,
                 prior_enrollment_text(record)] for record in prior]
        original_date = cell_text(fields.get("original_protocol_approval_date", {})) or MISSING_FIELD_VALUE
        rows.append([ORIGINAL_PROTOCOL_ROW, original_date, ORIGINAL_PROTOCOL_ENROLLMENT])
        self.table_caption(PRIOR_AMENDMENTS_TITLE)
        self.writer.simple_table([PRIOR_AMENDMENT_COLUMNS, *rows], fill=AI_TABLE_FILL)
        with_changes = [record for record in prior if record["changes"]]
        location = cell_text(fields.get("overview_location", {}))
        if with_changes:
            self.writer.paragraph(self.writer.styles["body"], text=OVERVIEW_BELOW)
        elif location:
            self.writer.paragraph(self.writer.styles["body"], text=location)
        for record in with_changes:
            identifier = AMENDMENT_WORD.sub("", cell_text(record["amendment_identifier"]))
            date = cell_text(record["sponsor_approval_date"])
            title = f"Overview of Changes in Amendment {identifier}" + (f" ({date})" if date else "")
            self.writer.paragraph(self.writer.styles["field_label"], text=title)
            self.changes_table(record["changes"], title)
        return True

    def objectives(self, objectives: list[JsonObject], level: int) -> None:
        """Each objective as a numbered heading (3.1.1 Primary Objective 1) with its estimands as a table."""
        for objective in objectives:
            statement = cell_text(objective["statement"])
            if not statement:
                continue
            self.heading(str(objective["title"]), level)
            if is_low_confidence(objective):
                self.low_confidence_note()
            self.highlighted(statement)
            tables = [rows for rows in map(estimand_rows, objective["estimands"]) if rows]
            for count, rows in enumerate(tables, 1):
                title = f"{ESTIMAND_TITLE} – {objective['title']}"
                self.table_caption(f"{title} (Estimand {count})" if len(tables) > 1 else title)
                self.writer.simple_table([["Estimand Characteristic", "Description"], *rows], fill=AI_TABLE_FILL,
                                         fill_columns=VALUE_COLUMN)

    def criterion(self, criterion: JsonObject) -> None:
        """One numbered paragraph; the criterion's further lines (sub-items) follow as bullets."""
        first, *rest = str(criterion["text"]["value"]).split("\n")
        self.writer.paragraph(self.writer.styles["body"], text=self.renumbered(f"{criterion['number']}. {first}"))
        for line in rest:
            self.writer.paragraph(self.writer.styles["bullet"], text=self.renumbered(line))

    def additional(self, node: JsonObject, level: int) -> None:
        landscape = self.landscape_blocks(node["content"])
        self.start_with_orientation_of(node["content"], landscape)
        self.heading(str(node["title"]), level)
        for block in node["content"]:
            self.block(block, landscape[id(block)])
        for child in node["subsections"]:
            self.additional(child, level + 1)

    def start_with_orientation_of(self, content: list[JsonObject], landscape: dict[int, bool]) -> None:
        """Switch page orientation before a heading so it lands on the same page as its first block."""
        self.writer.set_orientation(bool(content) and landscape[id(content[0])])

    def heading(self, title: str, level: int) -> None:
        """A numbered heading; its TOC entry gets the number Word will show for it."""
        shown = advance(self.heading_counters, level)
        self.toc_entries.append((level, shown, title))
        self.heading_title = title
        self.writer.numbered_heading(title, level)

    def status_text(self, status: str, content: list[JsonObject], custody: object = None) -> None:
        if status == "not_applicable":
            self.writer.paragraph(self.writer.styles["body"], text=NOT_APPLICABLE_TEXT)
        elif status == "missing" and is_no_basis(custody):
            self.review_note(NO_BASIS_NOTE)
        elif status == "missing" or (content and not has_text(content)):
            self.missing_text()
        elif is_low_confidence(content):
            self.low_confidence_note()

    def low_confidence_note(self) -> None:
        self.review_note(LOW_CONFIDENCE_NOTE)

    def review_note(self, text: str) -> None:
        run = self.writer.paragraph(self.writer.styles["body"]).add_run(text)
        run.font.highlight_color = WD_COLOR_INDEX.RED
        run.font.color.rgb = LOW_CONFIDENCE_TEXT_COLOR

    def missing_text(self) -> None:
        self.placeholder(MISSING_TEXT)

    def placeholder(self, text: str, style=None) -> None:
        """A paragraph where every [bracketed] span is red text."""
        paragraph = self.writer.paragraph(style or self.writer.styles["body"])
        for part in BRACKET_SPAN.split(text):
            if not part:
                continue
            run = paragraph.add_run(part)
            if BRACKET_SPAN.fullmatch(part):
                run.font.color.rgb = BRACKET_COLOR

    def highlighted(self, text: str) -> None:
        paragraph = self.writer.paragraph(self.writer.styles["body"])
        for part in BRACKET_SPAN.split(text):
            if not part:
                continue
            run = paragraph.add_run(part)
            if BRACKET_SPAN.fullmatch(part):
                run.font.color.rgb = BRACKET_COLOR
            else:
                run.font.highlight_color = WD_COLOR_INDEX.YELLOW

    def table_caption(self, title: str) -> None:
        """'Table N: title' above the next table, numbered in document order."""
        self.table_count += 1
        caption = self.writer.paragraph(self.writer.styles["caption"], text=f"Table {self.table_count}: {title}")
        caption.paragraph_format.keep_with_next = True

    def source_table_caption(self, table: JsonObject) -> None:
        """The source caption's title under this document's number, else the LLM's title, else the section heading."""
        caption = self.source_captions.get(id(table))
        if caption is None:
            self.table_caption(str(table.get("title") or self.heading_title))
            return
        label, title = SOURCE_TABLE_CAPTION.match(str(caption["text"]).strip()).groups()
        self.table_numbers.setdefault(label, self.table_count + 1)
        self.table_caption(self.renumbered(title.strip()))

    def renumber_table_references(self) -> None:
        """Point each 'Table N' in the text at the number its source caption now shows."""
        caption_style = self.writer.doc.styles[self.writer.styles["caption"]].style_id
        for paragraph in self.writer.body.iter(qn("w:p")):
            style = paragraph.find(qn("w:pPr") + "/" + qn("w:pStyle"))
            if style is not None and style.get(qn("w:val")) == caption_style:
                continue
            for text in paragraph.iter(qn("w:t")):
                if text.text:
                    text.text = TABLE_REFERENCE.sub(self.renumbered_reference, text.text)

    def renumbered_reference(self, match: re.Match[str]) -> str:
        number = self.table_numbers.get(match.group(1))
        return f"Table {number}" if number else match.group(0)

    def block(self, block: JsonObject, landscape: bool = False) -> None:
        if id(block) in self.caption_ids:
            return  # rewritten with this document's table number when its table is written
        if is_ai(block):
            self.ai_block(block)
            return
        rich_blocks = self.rich_blocks(block)
        if rich_blocks is None:
            self.plain_block(block)
            return
        self.writer.set_orientation(landscape)
        renumber_blocks(rich_blocks, self.shifted_numbers)
        for rich in rich_blocks:
            if isinstance(rich, Table):
                self.source_table_caption(block)
            write_block(self.writer, rich)

    def rich_blocks(self, block: JsonObject) -> list | None:
        xhtml = self.layout.get(str(block.get("source_index")))
        return None if xhtml is None else xhtml_to_blocks(xhtml, self.assets_dir)

    def landscape_blocks(self, content: list[JsonObject]) -> dict[int, bool]:
        """Which blocks go on landscape pages, keyed by id(block).

        A table or figure that sat on a landscape source page does, and so does the caption or heading
        right before it (a chain of marked blocks ending in one). Text after a table (footnotes) goes back to portrait.
        """
        rich = [None if is_ai(block) else self.rich_blocks(block) for block in content]
        marked = [blocks is not None and is_landscape(blocks) for blocks in rich]
        visual = [blocks is not None and any(isinstance(item, (Table, Figure)) for item in blocks) for blocks in rich]
        flags = {}
        leads_to_visual = False
        for index in reversed(range(len(content))):
            leads_to_visual = marked[index] and (visual[index] or leads_to_visual)
            flags[id(content[index])] = leads_to_visual
        return flags

    def plain_block(self, block: JsonObject, fill: str | None = None) -> None:
        if block["type"] == "table":
            self.source_table_caption(block)
            self.writer.simple_table([[self.renumbered(cell) for cell in row] for row in block["rows"]], fill=fill)
        elif block["type"] == "heading":
            self.writer.paragraph(self.writer.styles["field_label"], text=self.renumbered(block["text"]))
        else:
            self.writer.paragraph(self.writer.styles["body"], text=self.renumbered(block["text"]))

    def renumbered(self, text: str) -> str:
        return renumber_text(text, self.shifted_numbers)

    def ai_block(self, block: JsonObject) -> None:
        self.plain_block(block, fill=AI_TABLE_FILL)
        paragraph = self.writer.doc.paragraphs[-1] if block["type"] != "table" else None
        for run in paragraph.runs if paragraph is not None else []:
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def slot_text(value: object) -> str:
    if isinstance(value, dict) and "decode" in value:
        return str(value["decode"])
    if isinstance(value, dict):
        return f"{value['quantity']} {slot_text(value['unit'])}"
    return str(value)


def slot_displays(slot: object) -> list[str]:
    """Display text of a slot or list of slots: filled values, the source term of an `unsure` coded slot, or
    "Not applicable"."""
    if isinstance(slot, list):
        return [text for item in slot for text in slot_displays(item)]
    if not isinstance(slot, dict):
        return []
    if slot.get("status") == "filled":
        return [slot_text(slot["value"])]
    if slot.get("status") == "not_applicable":
        return [NOT_APPLICABLE_TEXT]
    if slot.get("status") == "unsure" and slot.get("raw_text"):
        return [str(slot["raw_text"])]
    return []


def has_filled_slot(node: object) -> bool:
    if isinstance(node, list):
        return any(has_filled_slot(item) for item in node)
    if not isinstance(node, dict):
        return False
    if "status" in node and "custody" in node:
        return node["status"] in ("filled", "unsure")
    return any(has_filled_slot(value) for value in node.values())


def enrollment_text(enrolled: JsonObject, scope: JsonObject) -> str:
    """'Approximately 40% enrolled globally'."""
    quantity = enrolled["quantity"].get("value")
    if quantity is None:
        return MISSING_FIELD_VALUE
    amount = f"{quantity:g}{'%' if enrolled['unit'].get('value') == 'percent' else ''}"
    return " ".join(part for part in ("Approximately", amount, "enrolled", cell_text(scope).lower()) if part)


def amendment_order(record: JsonObject) -> tuple[str, int]:
    """Sort key: approval date (ISO), then the amendment number, for records missing dates."""
    number = re.search(r"\d+", cell_text(record["amendment_identifier"]))
    return cell_text(record["sponsor_approval_date"]), int(number.group()) if number else 0


def prior_enrollment_text(record: JsonObject) -> str:
    """'Approximately 40% enrolled globally' from a prior amendment's free-text enrollment."""
    amount = cell_text(record["approximate_enrollment"])
    if not amount:
        return MISSING_FIELD_VALUE
    scope = cell_text(record["enrollment_scope"]).lower()
    lead = "" if amount.lower().startswith("approx") else "Approximately "
    return f"{lead}{amount} enrolled {scope}".strip()


def reasons_text(details: JsonObject) -> str:
    """Primary and secondary reasons; M11 says 'Not applicable' when there is no secondary reason."""
    primary = reason_list(details["primary_reasons"], details["primary_reason_other"])
    if not primary:
        return MISSING_FIELD_VALUE
    secondary = reason_list(details["secondary_reasons"], details["secondary_reason_other"]) or NOT_APPLICABLE_TEXT
    return f"Primary: {primary}\nSecondary: {secondary}"


def reason_list(reasons: list[JsonObject], other: JsonObject) -> str:
    """The coded reasons; an 'Other' reason is followed by its description."""
    names = [name for reason in reasons for name in slot_displays(reason)]
    other_text = cell_text(other)
    if other_text:
        names = [f"{name}: {other_text}" if name.lower() == "other" else name for name in names]
        if not any(name.lower().startswith("other") for name in names):
            names.append(f"Other: {other_text}")
    return "; ".join(names)


def impact_text(answer: JsonObject, explanation: JsonObject) -> str:
    yes_no = cell_text(answer)
    if not yes_no:
        return MISSING_FIELD_VALUE
    detail = cell_text(explanation)
    return f"{yes_no}\n{detail}" if yes_no == "Yes" and detail else yes_no


def estimand_rows(estimand: JsonObject) -> list[list[str]]:
    """Vertical M11 layout: one row per characteristic, then an Other Intercurrent Event | Strategy block."""
    rows = [[heading, cell_text(estimand[name])] for heading, name in zip(ESTIMAND_HEADINGS, ESTIMAND_COLUMNS)]
    events = [[cell_text(event["description"]), cell_text(event["strategy"])] for event in estimand["intercurrent_events"]]
    if not any(value for _, value in rows) and not any(any(event) for event in events):
        return []
    return [*rows, ["Other Intercurrent Event", "Strategy"], *events]


def cell_text(slot: object) -> str:
    return ", ".join(slot_displays(slot))


def filled_text(fields: JsonObject, paths: list[str], separator: str = " ") -> str:
    """The filled slots at `paths` joined by `separator` (items of a list slot by '; '); empty when none is filled."""
    values = [slot_values(fields, path) for path in paths]
    return separator.join(value for value in values if value)


def slot_values(fields: JsonObject, path: str) -> str:
    slot = fields
    for part in path.split("."):
        slot = slot.get(part, {}) if isinstance(slot, dict) else {}
    return "; ".join(slot_displays(slot))


def label_text(label: str, fields: JsonObject) -> str:
    """The filled text written under a 1.1.2 label; empty when nothing is filled."""
    paths = LABEL_SLOTS.get(label, [])
    if label == PARTICIPANTS_LABEL:
        return participants_sentence(fields)
    if label in LINE_PER_SLOT_LABELS:
        return filled_text(fields, paths, "\n")
    return filled_text(fields, paths)


def participants_sentence(fields: JsonObject) -> str:
    """'A Target of 120 participants will be enrolled.'; M11's choices stay in brackets where a slot is empty."""
    number = slot_values(fields, "number_of_participants")
    if not number:
        return ""
    qualifier = slot_values(fields, "participant_number_qualifier") or "[Target/Maximum]"
    basis = slot_values(fields, "participant_number_basis") or "[randomly assigned to trial intervention/enrolled]"
    return f"A {qualifier} of {number} participants will be {basis}."


def is_landscape(rich_blocks: list) -> bool:
    return any(isinstance(block, PageMark) and block.landscape for block in rich_blocks)


def has_text(content: list[JsonObject]) -> bool:
    """False for a section holding only figures, which M11 validators read as empty."""
    return any(not block.get("has_image") for block in content)


def is_lead_in(block: JsonObject) -> bool:
    return block["type"] == "paragraph" and block["text"].rstrip().endswith(":")


def is_title_field(item: JsonObject) -> bool:
    return item["v10_source"]["section_id"] == TITLE_PAGE_UNMAPPED_ID


def is_ai(block: JsonObject) -> bool:
    return block["custody"]["origin"] in AI_ORIGINS


def is_no_basis(custody: object) -> bool:
    """The model was asked to draft this and found nothing in the source to infer it from."""
    return isinstance(custody, dict) and custody.get("origin") in AI_ORIGINS \
        and custody.get("confidence") == NO_BASIS_CONFIDENCE


def is_low_confidence(node: object) -> bool:
    """True when any AI-written block, slot or record in `node` was written without a basis in the source."""
    if isinstance(node, list):
        return any(is_low_confidence(item) for item in node)
    if not isinstance(node, dict):
        return False
    custody = node.get("custody")
    if isinstance(custody, dict) and custody.get("origin") in AI_ORIGINS and custody.get("confidence") == LOW_CONFIDENCE:
        return True
    return any(is_low_confidence(value) for key, value in node.items() if key != "custody")


def label_slots(label: str, fields: JsonObject) -> list[object]:
    """The slots whose values are written under a 1.1.2 label."""
    slots = []
    for path in LABEL_SLOTS.get(label, []):
        slot: object = fields
        for part in path.split("."):
            slot = slot.get(part, {}) if isinstance(slot, dict) else {}
        slots.append(slot)
    return slots


def render_docx(protocol: JsonObject, layout: dict[str, str], assets_dir: Path,
                template: Path, out_path: Path, protocol_label: str = "") -> None:
    DocxRenderer(template, layout, assets_dir, protocol_label).render(protocol, out_path)
