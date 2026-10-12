"""AI Understanding / Normalization — the stage between raw OCR structuring
(`extractors.parse_prescription_entities`, which decides WHICH lines belong
to WHICH medicine) and the fields the Review page shows.

This stage does two things, deliberately separated from structuring:

1. Cleans a medicine NAME of a stray, isolated leading OCR-noise symbol
   ("& Metformin" -> "Metformin") that structuring's own block-detection
   correctly left alone (a symbol is never mistaken for a list marker by
   `extractors._LIST_MARKER`), but that has no business being part of a
   drug name either.
2. Judges, from purely deterministic signals, how much to trust each
   field — a `CERTAINTY_*` category, never a fabricated probability.

What this deliberately does NOT do: spelling correction. There is no drug
formulary or dictionary in scope (see CLAUDE.md's "no external medical
database integration" / "no full DRAP integration"), so "correcting"
"Metformln" to "Metformin" would mean guessing from nothing but a hunch —
exactly the "never invent" rule this whole pipeline is built around. A name
that looks OCR-damaged is left exactly as structuring produced it and
flagged with lower certainty for the patient to fix on the Review page.

Printed and handwritten prescriptions reach this stage identically: it
operates on the plain `{"medicine", "dosage", "frequency", "duration"}`
dicts `parse_prescription_entities` already returns for both, and knows
nothing about which OCR engine produced the text behind them.
"""

from __future__ import annotations

import re

CERTAINTY_HIGH = "high"
CERTAINTY_UNCERTAIN = "uncertain"
CERTAINTY_AMBIGUOUS = "ambiguous"
CERTAINTY_MISSING = "missing"

# A single, ISOLATED leading symbol before a medicine name — separated from
# the rest of the value by whitespace, the same shape a corrupted list
# marker or a hallucinated OCR glyph takes. Never touched mid-name, so a
# real combination-drug name ("Tylenol & Codeine") is never damaged, and
# never touched when the symbol isn't its own token (nothing here would
# match a genuine "A&E" style abbreviation fused without a space).
#
# A circled or numbered list marker OCR reads as junk comes in more shapes than
# one bare symbol: "@)" / "&," (symbols, 1-3 of them), or a letter/digit
# with a closing bracket ("G)", "1]"). Seen directly on scans of a
# prescription: "@) Amoxicillin", "G) Vitamin D3", "&, Losartan". "(" is
# excluded here — `_PAREN_MARKER_PREFIX` below owns that shape — and the
# whitespace requirement keeps a name that merely STARTS with a symbol or a
# bracketed letter fused to it ("(Extra)", "+Calcium") untouched.
_LEADING_NOISE_SYMBOL = re.compile(r"^(?:[^\w\s(]{1,3}|[A-Za-z0-9]{1,2}[)\]])\s+")
# A corrupted list marker that kept an opening "(" — a circled digit read
# as a stray parenthesis, with whatever came after it (a misread digit, a
# stray letter) glued or spaced onto the real name ("( D Paracetamol",
# "( A_Cetirizing"). Safe to strip ENTIRELY, unconditionally: no real
# medicine name legitimately starts with "(" at all (a real parenthetical,
# like "Panadol (Extra)", is never at position 0), so there is no
# legitimate name this could ever damage — unlike a bare "<letter>."
# prefix (see below), which is deliberately NOT stripped here.
_PAREN_MARKER_PREFIX = re.compile(r"^\(\s*[A-Za-z_]{0,3}[\s_]+")
# Deliberately NOT STRIPPED here: a bare "<single letter>." prefix ("G.
# Amoxicillin" — item 2's marker misread as a letter). The exact same
# shape is also a REAL product name ("B. Complex" — a genuine, commonly
# prescribed vitamin B product, not a typo or OCR noise; see
# `test_b_complex_keeps_its_own_period_unlike_a_misread_list_number` in
# `test_document_extractors.py`). There is no reliable way to tell these
# apart from the text shape alone — only a drug-name list (fuzzy-matching
# "Amoxicillin" as a known name on its own, the way "Complex" alone is
# NOT) could safely resolve this, which is exactly the later suggested-
# corrections step's job, not this one's. Guessing wrong here would mean
# either breaking a real product name or leaving real junk in place.
# What IS safe, and applied below in `_medicine_certainty`: flagging the
# shape as UNCERTAIN without touching the text at all — "B. Complex"
# stays "B. Complex" (just no longer blindly HIGH), "G. Amoxicillin"
# stays "G. Amoxicillin" too, but now honestly marked as needing a
# second look either way.
_SHORT_PREFIX_MARKER_SHAPE = re.compile(r"^[A-Za-z]\.\s+[A-Z]")
# A 1-3 letter LOWERCASE token in front of a capitalised name ("db Metformin",
# "ile Tab. Metformin" once the form word is gone) is almost always a list
# marker OCR read as letters — but "iv Fluid" style names exist, so like the
# bare "G." shape above it is only ever FLAGGED, never stripped.
_LOWERCASE_PREFIX_SHAPE = re.compile(r"^[a-z]{1,3}\s+[A-Z][a-z]")
# A name that still opens with a non-alphanumeric character after the
# cleanup above ran is not fully recovered — structuring's own validity
# check already requires 2+ real letters to accept a row at all, so this
# means real letters ARE present, just behind noise nothing here removes.
_SUSPICIOUS_LEADING_CHAR = re.compile(r"^[^A-Za-z0-9]")
# Symbols that never belong ANYWHERE in a genuine medicine/dosage/frequency/
# duration value — never "-", "/", ".", "," or "%", all of which are
# legitimate inside a real value ("1-0-1", "70-100", "600mg/400mg") — but a
# stray "(", "_", "#" etc. mid-value is exactly the shape a circled digit,
# an underline stroke, or another corrupted list-marker artifact leaves
# behind when it lands INSIDE a field instead of before it (the leading
# case above already catches the "before" case; this catches "embedded").
# Applied to every field, not just medicine: a corrupted symbol doesn't
# only ever land in front of a drug name.
_EMBEDDED_JUNK = re.compile(r"[(){}\[\]_#@~^*|]")
# The same shape `extractors._GARBLED_NUMBER` looks for (duplicated, not
# imported: `extractors.py` already imports THIS module, so the reverse
# import would be circular) — a digit run immediately followed by comma or
# period noise, the signature a genuinely-present but unrecoverable
# strength/dose leaves behind ("D3,000. IN." for what was probably "1000
# IU"). Finding this in the row's own source text is what lets a
# completely empty dosage be told apart from one that was never written at
# all: "missing" means nothing was there; "ambiguous" means something WAS
# there, is flagged for the patient to read off the original and fill in
# themselves, and must never be silently reported as if it simply doesn't
# exist.
_GARBLED_NUMBER_SHAPE = re.compile(r"\d{2,}[,.]")

# Guard against TrOCR's "confident hallucination" failure (see `ocr.py`'s
# module docstring): a severely degraded crop can make the model fall back
# to fluent, grammatically real, but entirely fabricated text memorised
# from its own training data (observed directly: real Wikipedia navigation
# boilerplate) instead of failing cleanly — text that per-token confidence
# alone can score HIGH, and that `ocr._looks_degenerate` cannot catch
# either, since it is real words, not symbol noise. This is the second,
# field-level layer: a real medicine name or dosage is NEVER a run of
# common English connective words, and is never more than a handful of
# short tokens — nothing legitimate in either field is a full sentence.
_CAMEL_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _words(value: str) -> list[str]:
    """Splits on whitespace AND a lower->upper letter boundary, so a
    hallucination with no real spacing between its own fragments (observed
    directly: "hereRelated changesUpload", read off adjacent navigation
    links with nothing to separate them) still tokenizes into separate
    words instead of hiding as one giant token."""
    spaced = _CAMEL_SPLIT.sub(" ", value)
    return [w for w in (t.strip(".,;:!?'\"()") for t in spaced.split()) if w]


_ENGLISH_FUNCTION_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "this",
        "that",
        "these",
        "those",
        "and",
        "or",
        "but",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "from",
        "with",
        "by",
        "as",
        "it",
        "its",
        "be",
        "been",
        "has",
        "have",
        "had",
        "not",
        "no",
        "you",
        "your",
        "he",
        "she",
        "they",
        "we",
        "i",
    }
)
_MIN_WORDS_FOR_SENTENCE_CHECK = 4
_MIN_FUNCTION_WORD_HITS = 2
# A real medicine name is a product/generic name, at most a handful of
# tokens even for a combination ("Amoxicillin Clavulanate", "Vitamin B
# Complex") — nothing legitimate in either field runs longer than this.
_MAX_PLAUSIBLE_MEDICAL_WORDS = 5


def _looks_like_fabricated_prose(value: str) -> bool:
    """A full-sentence shape: either 2+ common English function words among
    its tokens, or simply too many tokens for a real name/dosage to ever
    legitimately have — either is enough on its own."""
    words = _words(value)
    if len(words) < _MIN_WORDS_FOR_SENTENCE_CHECK:
        return False
    hits = sum(1 for w in words if w.lower() in _ENGLISH_FUNCTION_WORDS)
    return hits >= _MIN_FUNCTION_WORD_HITS or len(words) > _MAX_PLAUSIBLE_MEDICAL_WORDS


_HAS_DIGIT = re.compile(r"\d")
_DOSAGE_UNIT_WORD = re.compile(
    r"\b(?:mg|mcg|g|ml|iu|units?|tabs?|tablets?|caps?|capsules?|drops?|puffs?|sprays?|mmol|mEq)\b",
    re.IGNORECASE,
)


def _dosage_lacks_medical_shape(value: str) -> bool:
    """A real dosage always has a number and/or a recognised unit word
    ("500 mg", "1-0-1", "2 tablets") — flags one that has neither, which is
    exactly the shape a fabricated or misattributed value takes landing in
    a field that should be a number."""
    return bool(value) and not (_HAS_DIGIT.search(value) or _DOSAGE_UNIT_WORD.search(value))


# A strength read off a scan can keep its number and lose its unit to OCR
# ("500 ag", "500 mq", "20 mq" for "500 mg", "20 mg") — still a plausible
# looking value, so nothing else flags it. A number followed by a unit that
# is not one a dosage can have is an OCR misread, so it is flagged UNCERTAIN
# for the patient to check against the original (never "corrected": "mq" could
# in principle be anything, and guessing a dose unit is exactly the kind of
# silent fix this pipeline refuses to make).
_KNOWN_DOSAGE_UNITS = frozenset(
    {"mg", "mcg", "µg", "ug", "g", "gm", "kg", "ml", "cc", "l", "iu", "u", "unit", "units", "%"}
    | {"mmol", "meq", "drop", "drops", "puff", "puffs", "spray", "sprays", "tab", "tabs"}
    | {"tablet", "tablets", "cap", "caps", "capsule", "capsules", "sachet", "sachets", "spoon"}
    | {"spoons", "tsp", "tbsp", "vial", "vials", "amp", "ampoule", "ampoules", "patch"}
    | {"application", "applications", "dose", "doses"}
)
_NUMBER_THEN_UNIT = re.compile(r"\d[\d.,]*\s*([A-Za-zµ%]+(?:\s*/\s*[A-Za-zµ]+)?)")


def _dosage_has_unrecognised_unit(value: str) -> bool:
    for m in _NUMBER_THEN_UNIT.finditer(value):
        parts = re.sub(r"\s+", "", m.group(1).lower()).split("/")
        if any(part and part not in _KNOWN_DOSAGE_UNITS for part in parts):
            return True
    return False


CERTAINTY_WEIGHT: dict[str, float] = {
    CERTAINTY_HIGH: 1.0,
    CERTAINTY_UNCERTAIN: 0.6,
    CERTAINTY_AMBIGUOUS: 0.3,
}


def _clean_medicine_name(name: str) -> str:
    return _PAREN_MARKER_PREFIX.sub("", _LEADING_NOISE_SYMBOL.sub("", name))


def _medicine_certainty(original: str, cleaned: str) -> str:
    if not cleaned:
        return CERTAINTY_MISSING
    if _SUSPICIOUS_LEADING_CHAR.match(cleaned) or _EMBEDDED_JUNK.search(cleaned):
        return CERTAINTY_AMBIGUOUS
    if _looks_like_fabricated_prose(cleaned):
        return CERTAINTY_AMBIGUOUS
    if cleaned != original:
        # A noise symbol WAS present and removed: the name is clean now,
        # but the underlying OCR for this line was imperfect.
        return CERTAINTY_UNCERTAIN
    if _SHORT_PREFIX_MARKER_SHAPE.match(cleaned) or _LOWERCASE_PREFIX_SHAPE.match(cleaned):
        # The exact shape `_PAREN_MARKER_PREFIX` deliberately does NOT
        # touch ("G. Amoxicillin" — possibly a corrupted list number,
        # possibly not): never rewritten (still no reliable way to tell
        # it apart from a real product name like "B. Complex"), but never
        # silently trusted as a clean, fully-confident reading either.
        # Flagged uncertain — not ambiguous — because the REST of the
        # name, if this prefix genuinely is noise, reads cleanly; this is
        # about the prefix specifically, not the whole value being hard
        # to make out.
        return CERTAINTY_UNCERTAIN
    return CERTAINTY_HIGH


def _field_certainty(value: str, *, source: str | None, field_name: str) -> str:
    """Honest by construction: a non-empty value only ever earns HIGH if it
    contains nothing a corrupted OCR read would leave behind. Never a
    fabricated probability, never "high" just because a value happens to
    be non-empty (that was the actual bug: a wrong-but-non-empty reading —
    "daily" truncated from "three times daily" — used to look exactly as
    trustworthy as a correct one).

    Two DIFFERENT reasons a value can be empty (dosage only, for now — see
    `_GARBLED_NUMBER_SHAPE`'s docstring): genuinely never written at all
    (stays "missing") versus something WAS there but too corrupted for any
    of this module's patterns to recover ("ambiguous" — flagged for the
    patient to read off the original and fill in, never silently reported
    as if nothing exists).
    """
    if not value:
        if field_name == "dosage" and source and _GARBLED_NUMBER_SHAPE.search(source):
            return CERTAINTY_AMBIGUOUS
        return CERTAINTY_MISSING
    if _EMBEDDED_JUNK.search(value):
        return CERTAINTY_AMBIGUOUS
    if _looks_like_fabricated_prose(value):
        return CERTAINTY_AMBIGUOUS
    if field_name == "dosage" and _dosage_lacks_medical_shape(value):
        return CERTAINTY_AMBIGUOUS
    if field_name == "dosage" and _dosage_has_unrecognised_unit(value):
        return CERTAINTY_UNCERTAIN
    return CERTAINTY_HIGH


def normalize_prescription_rows(
    entities: list[dict[str, str]],
    sources: list[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """`parse_prescription_entities`'s output, cleaned and scored.

    `sources[i]`, when given, is the raw OCR block entity `i` was built
    from (see `extractors._prescription_entities_with_sources`) — used only
    to tell a genuinely blank field apart from one whose source was too
    garbled to recover (see `_field_certainty`). `None` (the doctor-
    generated path, `service.create_doctor_prescription`: there is no OCR
    block at all for already-typed input) simply skips that one check;
    everything else about scoring is identical either way.

    Returns `(rows, certainty)`, two parallel lists of the same length:
    `rows[i]` is exactly the `{"medicine", "dosage", "frequency",
    "duration"}` shape `Extraction.rows` has always had (so nothing
    downstream — `OcrExtraction` persistence, the Review page — needs to
    change shape); `certainty[i]` carries the same four keys, each mapped
    to a `CERTAINTY_*` category rather than a value.
    """
    rows: list[dict[str, str]] = []
    certainty: list[dict[str, str]] = []
    for index, entity in enumerate(entities):
        source = sources[index] if sources is not None and index < len(sources) else None
        original_name = entity.get("medicine", "")
        cleaned_name = _clean_medicine_name(original_name)
        row = {**entity, "medicine": cleaned_name}
        row_certainty = {"medicine": _medicine_certainty(original_name, cleaned_name)}
        for field_name in ("dosage", "frequency", "duration"):
            row_certainty[field_name] = _field_certainty(
                row.get(field_name, ""), source=source, field_name=field_name
            )
        rows.append(row)
        certainty.append(row_certainty)
    return rows, certainty


def aggregate_confidence(certainty_rows: list[dict[str, str]]) -> float | None:
    """A single, honestly-derived document-level summary — the mean
    certainty weight across every field that was actually FOUND (a field
    genuinely absent from the document, like duration on a PRN medicine, is
    not a reading failure, so it never counts against the score). `None`
    when there is nothing to score, matching how `OcrJob.overall_confidence`
    already treats "no value yet"."""
    scored = [
        CERTAINTY_WEIGHT[level]
        for row in certainty_rows
        for level in row.values()
        if level != CERTAINTY_MISSING
    ]
    return round(sum(scored) / len(scored), 3) if scored else None
