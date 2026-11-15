"""Names for values of the protocol JSON that the Word renderer and the FHIR export both read."""

from converter.model import JsonObject

HIGH_CONFIDENCE = "high"
LOW_CONFIDENCE = "low"
VERY_LOW = "very_low"  # output the source gives no basis for
NO_BASIS_CONFIDENCE = "none"
CONFIDENCE_LEVELS = (HIGH_CONFIDENCE, "medium", LOW_CONFIDENCE, VERY_LOW)
AI_ORIGINS = ("ai_generated", "ai_transformed")
MISSING_TEXT = "[NOT EXTRACTED: no content found in the source protocol]"
NOT_APPLICABLE_TEXT = "Not applicable"

# Fixed M11 wording the validator checks; the source's own lead-in sentence is replaced, not repeated
LEAD_INS = {
    "5.2": "To be eligible to participate in this trial, an individual must meet all the following criteria:",
    "5.3": "An individual who meets any of the following criteria will be excluded from participation in this trial:",
    "11.1": "This trial will be conducted in accordance with the protocol and with the following:",
}


def is_lead_in(block: JsonObject) -> bool:
    return block["type"] == "paragraph" and block["text"].rstrip().endswith(":")


def is_ai(block: JsonObject) -> bool:
    return block["custody"]["origin"] in AI_ORIGINS
