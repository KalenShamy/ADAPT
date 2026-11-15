"""Read title-page fields (label and value pairs) and collect the title values to keep."""

import re
from collections import Counter

from converter.extract.pdf_reader import rows_to_paragraphs


MAX_LABEL_LENGTH = 80
MIN_TWO_COLUMN_LABELS = 3  # label rows needed before a title page is read as label | value columns
ELECTRONIC_SIGNATURE_VALUE = re.compile(r"^see appended electronic signature page\.?$", re.IGNORECASE)
DATE_LABEL = re.compile(r"\bdate( of issue)?:?$", re.IGNORECASE)
MEDICAL_MONITOR_STATEMENT = re.compile(r"^medical monitor name and contact information (can|will)\b", re.IGNORECASE)
COVER_HEADING = re.compile(r"^(title page|(amended )?clinical (trial|study) protocol( \d+)?)$", re.IGNORECASE)

# Title-page labels seen in sponsor PDFs, mapped to a CPT field key.
TITLE_FIELD_PATTERNS = [
    ("confidentiality", r"(sponsor )?confidential(ity)?( statement)?"),
    ("title", r"(protocol|full|official|study|trial)?\s*title"),
    ("short_title", r"(brief|short)\s+title"),
    ("acronym", r"(trial |study )?acronym|(trial|study) name(/acronym)?"),
    ("protocol_number", r"(clinical (trial|study) )?protocol (number|identifier|no\.?)|sponsor protocol identifier|study (number|identifier)|protocol identifier|trial id"),
    ("amendment_number", r"amendment (number|identifier|no\.?)"),
    ("version", r"(protocol )?version( number)?"),
    ("date", r"(version |approval |protocol |release )?date|date of issue"),
    ("compound", r"compound( number| name)?(/name)?( \(.*\))?|investigational product( code)?\(?s?\)?|substance name|test product|study (drug|intervention)"),
    ("phase", r"(study|trial|clinical) phase|phase"),
    ("sponsor", r"sponsor( name)?( and (legal )?(registered )?address)?"),
    ("sponsor_address", r"legal registered address|sponsor address"),
    ("manufacturer", r"manufacturer"),
    ("regulatory_ids", r"regulatory agency identifier number\(?s?\)?|regulatory or clinical trial identifier\(?s?\)?|registry identifiers?|regulatory identifier"),
    ("registry_id", r"other|(ind|nct|who|eudamed|eudract|eu ct|ctis|isrctn)( trial)?( number| no\.?)?|eu trial number|eu ct number|eudract/eu ct number"),
    ("signatory", r"sponsor signatory"),
    ("medical_monitor", r"medical (monitor|expert).*|(study )?medical\s*(/\s*study)?\s*director(\s*/\s*medical monitor)?|monitoring team.*"),
    ("primary_intervention", r"primary study intervention\(s\)"),
]


def title_fields(rows):
    """Split title-page text into [label, value] pairs; unlabeled paragraphs get label None."""
    value_x = value_column_x(rows)
    if value_x is not None:
        return two_column_fields(rows, value_x)
    fields = []
    for para in rows_to_paragraphs(rows, break_on_labels=True):
        for spans in split_at_labels(para.spans):
            text = "".join(s.text for s in spans).strip()
            label, value = split_label(spans)
            if label:
                fields.append([label, value])
            elif fields and fields[-1][0] and not fields[-1][1]:
                fields[-1][1] = text
            elif text:
                fields.append([None, text])
    return fields


def split_at_labels(spans):
    """Split a paragraph before each bold 'Label:' that follows other text (two fields on one line)."""
    groups = [[]]
    for s in spans:
        follows_text = any(x.text.strip() for x in groups[-1])
        if s.bold and s.text.strip().endswith(":") and follows_text and not groups[-1][-1].bold:
            groups.append([])
        groups[-1].append(s)
    return groups


def is_label_text(text):
    return text.endswith(":") or canonical_label(text) is not None


def value_column_x(rows):
    """Left edge of the value column when the title page is laid out as label | value columns."""
    starts = Counter(round(piece[0]) for row in rows for piece in row.pieces[1:])
    labels = sum(1 for row in rows if len(row.pieces) > 1 and is_label_text(row.first_piece_text()))
    if not starts or labels < MIN_TWO_COLUMN_LABELS:
        return None
    x, count = starts.most_common(1)[0]
    return x if count >= MIN_TWO_COLUMN_LABELS else None


def two_column_fields(rows, value_x):
    """Pair labels in the left column (which may wrap over several rows, ending in ':') with the
    text beside them in the right column."""
    fields = []
    label_open = False  # the last label hasn't reached its closing ':' yet
    sentence_open = False  # the last unlabeled sentence wraps onto the next row
    for row in rows:
        left = " ".join("".join(s.text for s in spans).strip() for x, spans in row.pieces if x < value_x - 5).strip()
        right = " ".join("".join(s.text for s in spans).strip() for x, spans in row.pieces if x >= value_x - 5).strip()
        if right or not left:
            sentence_open = False
        if left and not right and sentence_open:
            fields[-1][1] += " " + left
            sentence_open = not left.endswith(".")
        elif left and not right and (len(left) > MAX_LABEL_LENGTH or MEDICAL_MONITOR_STATEMENT.match(left)
                                     or (COVER_HEADING.match(left) and not label_open)):
            fields.append([None, left])
            label_open = False
            sentence_open = bool(MEDICAL_MONITOR_STATEMENT.match(left)) and not left.endswith(".")
        elif left and label_open and not starts_new_label(fields[-1], left):
            fields[-1][0] += " " + left
            fields[-1][1] = (fields[-1][1] + " " + right).strip()
        elif left:
            fields.append([left, right])
            label_open = True
        elif right and fields and fields[-1][0]:
            fields[-1][1] = (fields[-1][1] + " " + right).strip()
        else:
            fields.append([None, right])
        if fields[-1][0] and fields[-1][0].endswith(":"):
            label_open = False
    out = []
    for label, value in fields:
        if label and not is_label_text(label):
            out.append([None, f"{label} {value}".strip()])
        else:
            out.append([label, value])
    return out


def starts_new_label(open_field, next_text):
    """A wrapped label continues on the next row unless it already has its value or both texts are labels."""
    open_label, open_value = open_field
    if canonical_label(open_label) and canonical_label(next_text):
        return True
    return bool(open_value) and ":" not in next_text and not next_text.startswith("(")


def split_label(spans):
    """('Label:', value) when a paragraph starts with a field label, else (None, None)."""
    text = "".join(s.text for s in spans).strip()
    bold_prefix = ""
    for s in spans:
        if not s.bold:
            break
        bold_prefix += s.text
    rest = text[len(bold_prefix.strip()):].strip() if text.startswith(bold_prefix.strip()) else ""
    bold_prefix = bold_prefix.strip()
    if bold_prefix.endswith(":") and len(bold_prefix) <= MAX_LABEL_LENGTH:
        return bold_prefix, rest
    if bold_prefix and rest.startswith(":") and len(bold_prefix) < MAX_LABEL_LENGTH:
        return bold_prefix + ":", rest[1:].strip()
    match = re.match(r"^([A-Z][\w ()/'.-]{2,60}):\s*(.*)$", text)
    if match and canonical_label(match.group(1)):
        return match.group(1) + ":", match.group(2)
    return None, None


def canonical_label(label):
    plain = label.rstrip(":").strip().lower()
    for key, pattern in TITLE_FIELD_PATTERNS:
        if re.fullmatch(pattern, plain):
            return key
    return None


REGISTRY_LABEL = re.compile(r"\b(IND|NCT|WHO|EUDAMED|EudraCT|EU CT|CTIS|ISRCTN|EU trial number)\b[\s:#]*(?:number|no\.?)?[\s:#]*([A-Z0-9][\w\-/]*\d[\w\-/]*)", re.IGNORECASE)


def collect_title_values(doc, notes=None):
    """({field key: value}, [(label, value) of fields without a known key]) from the title rows and tables."""
    notes = [] if notes is None else notes
    values, others = {}, []
    pairs = [tuple(pair) for pair in title_fields(doc.title_rows)]
    for table in doc.title_tables:
        for row in table.grid:
            texts = [" ".join(p.text for p in cell["paragraphs"]).strip() for cell in row if cell]
            if len(texts) < 2:
                continue
            label, value = texts[0], " ".join(texts[1:]).strip()
            if not label and pairs and pairs[-1][0]:
                pairs[-1] = (pairs[-1][0], f"{pairs[-1][1]} {value}".strip())
            elif label and not value:
                pairs.append(split_known_label(label))
            elif label:
                pairs.append((label, value))
    for label, value in pairs:
        key = canonical_label(label) if label else None
        shares_line_with_registry_ids = bool(label) and not key and label_after_registry_ids(label) is not None
        if not label and value and canonical_label(value) == "medical_monitor":
            values.setdefault("medical_monitor", value)
        elif not label and COVER_HEADING.match(value):
            continue
        elif shares_line_with_registry_ids:
            # The value also holds the sponsor line printed beside the ids, so it is not a usable compound.
            add_registry_ids(values, label)
        elif label and DATE_LABEL.search(label) and ELECTRONIC_SIGNATURE_VALUE.match(value):
            notes.append(f"title page: {label.rstrip(':')} is only '{value}'; Version Date left as placeholder")
        elif key in ("sponsor", "sponsor_address"):
            values["sponsor"] = join_sponsor(values.get("sponsor"), value)
        elif key == "registry_id":
            values["regulatory_ids"] = "; ".join(x for x in (values.get("regulatory_ids"), f"{label.rstrip(':')}: {value}") if x)
        elif key:
            if value:
                values.setdefault(key, value)
        elif label:
            others.append((label, value))
        elif value:
            others.append((None, value))
    return values, others


def split_known_label(text):
    """(label, value) when a cell holds a known label followed by its value, else (text, '')."""
    words = text.split()
    for count in range(len(words) - 1, 0, -1):
        prefix = " ".join(words[:count])
        if canonical_label(prefix):
            return prefix, " ".join(words[count:])
    return text, ""


def label_after_registry_ids(label):
    """The label left once registry ids are removed from `EU CT: 123 IND Number: 456 Compound:`, or None."""
    if not registry_rows(label):
        return None
    remainder = REGISTRY_LABEL.sub("", label).strip()
    return remainder if canonical_label(remainder) else None


def add_registry_ids(values, text):
    known = values.get("regulatory_ids", "")
    for label, identifier in registry_rows(text):
        if identifier not in known:
            known = "; ".join(x for x in (known, f"{label}: {identifier}") if x)
    values["regulatory_ids"] = known


def join_sponsor(name, address):
    """Name and address of the sponsor; the address often repeats the name."""
    if name and address.startswith(name):
        return address
    return " ".join(part for part in (name, address) if part)


def registry_rows(text):
    return [(match.group(1).upper(), match.group(2)) for match in REGISTRY_LABEL.finditer(text)]


def amendment_state(values):
    """'none', 'first' or 'later' from the amendment number the source gives."""
    digits = re.search(r"\d+", values.get("amendment_number", ""))
    if not digits:
        return "none"
    return "first" if int(digits.group()) == 1 else "later"
