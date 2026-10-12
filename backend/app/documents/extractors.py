"""Document-type-specific extraction — the ONLY part of the pipeline that
varies by document type.

OCR is common (`ocr.py`); this module turns its text into *proposed* field
values for the patient to review. Nothing here is trusted as fact: every value
is a proposal until the patient confirms it.

What each type extracts is limited to what the shared schema already models —
no clinical field is invented:

* prescription  -> `medicine`, `dosage`, `frequency`, `duration` per medicine
                   (the four fields `ocr_extraction` was designed around, PRD
                   Objective 2.1)
* lab report    -> `test_name`, `result`, `unit`, `reference_range` per result
                   (the columns of the shared `lab_result` table)
* vaccination   -> nothing beyond the OCR text and the document date
* other         -> nothing beyond the OCR text and the document date

Extraction is deterministic (regular expressions over the OCR text), so it is
testable, never calls a model with attacker-controlled text as instructions,
and cannot be steered by a document that says "ignore previous instructions".
It is conservative: a line that does not clearly look like a medicine or a
lab result is skipped rather than guessed at, and the patient can add or fix
rows during review.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from ..db import types as dbtypes
from ..db.models import DOC_TYPE_LAB, DOC_TYPE_OTHER, DOC_TYPE_RX, DOC_TYPE_VACCINE
from . import groups, normalization

# The field names of each document type's PRIMARY table (the medicines, the lab
# results, the vaccine doses), in display order. A document's other tables —
# diagnosis, symptoms, vital signs, allergies — are `groups.py`'s business; the
# complete list of names `ocr_extraction.field_name` may hold is `groups.ALL_FIELDS`.
FIELDS: dict[str, tuple[str, ...]] = {
    doc_type: (primary.fields if primary is not None else ())
    for doc_type, primary in groups.PRIMARY.items()
}

# The field that must be non-empty for a row to count as a real entry — for the
# types whose primary table must have at least one row (a prescription needs a
# medicine, a lab report a result).
REQUIRED_FIELD: dict[str, str] = {
    doc_type: primary.required
    for doc_type, primary in groups.PRIMARY.items()
    if primary is not None and groups.min_rows(doc_type, primary) and primary.required
}

MAX_ROWS = 100
MAX_VALUE_LEN = 255  # ocr_extraction.extracted_value / corrected_value are varchar(255)


@dataclass
class Extraction:
    rows: list[dict[str, str]] = field(default_factory=list)
    document_date: date | None = None
    # Per-row, per-field certainty (see `normalization.py`) — `None` for
    # extractors that don't produce it (lab, vaccination, other), never a
    # placeholder value. When present it is the same length/shape as `rows`.
    field_certainty: list[dict[str, str]] | None = None


# ── Shared helpers ───────────────────────────────────────────────────────
# A dosage-form word (also used to strip "Tab." etc. off a medicine name in
# the prescription section below): shared here because `_LIST_MARKER` needs
# it too.
_FORM = (
    r"(?:tab(?:let)?s?|cap(?:sule)?s?|syp|syrup|inj(?:ection)?|susp(?:ension)?|"
    r"cream|oint(?:ment)?|drops?|gel|lotion|inhaler|sachet)"
)
# The alternatives, in order: (1) a numbered/lettered marker ("1.", "2)"),
# (2) a bullet, (3) a numbered marker whose digit OCR turned into a single
# stray letter AND whose "." became a "," (observed: "2." -> "a,") — a comma
# there is not something real prescription text ever writes, so it is a safe,
# narrow tell that this was a list marker, not part of a word (the lookahead
# requires a capitalised word next, as a medicine name would be), and (4) a
# numbered marker that kept its digit but lost its own "." / ")" while the
# ABBREVIATED form word right after it kept ITS period (observed: "2." ->
# "2 Tab. Atorvastatin..."). The lookahead requires that trailing period
# specifically: a genuine dosage instruction ("2 tablets to be taken daily")
# never has one there, so this cannot mistake a real quantity instruction
# for a list marker, even when one legitimately opens a line.
_LIST_MARKER = re.compile(
    rf"^\s*(?:\d{{1,2}}\s*[.)]\s+|[-•*·]\s+|[A-Za-z]\s*,\s+(?=[A-Z])|\d{{1,2}}\s+(?={_FORM}\.))",
    re.IGNORECASE,
)
# A bare 1-2 digit list number that kept neither its punctuation nor a form
# word ("1 Metformin..." for "1. Metformin..."), only usable once a name is
# already confirmed to start right after it (a real digit-led drug name like
# "3TC" has no space before its letters, so this never touches one).
_BARE_NUM_MARKER = re.compile(r"^\d{1,2}\s+(?=[A-Z])")
_WS = re.compile(r"\s+")


def _clean(value: str) -> str:
    return _WS.sub(" ", value).strip(" \t-:;,|")[:MAX_VALUE_LEN]


def _lines(text: str) -> list[str]:
    """Non-blank lines, with internal whitespace collapsed to single spaces.

    Every field pattern below (`_STRENGTH`, `_FREQUENCY`, `_FORM_NEAR_START`,
    ...) allows at most ONE space between tokens (`\\s?`) — deliberately,
    so two genuinely separate numbers are never read as one. Real OCR,
    especially handwritten, routinely produces irregular double/triple
    spaces between words (wide letter-spacing, a segmentation gap) that
    would otherwise make every one of those patterns silently stop matching
    at all, discarding an entity whose text was actually read correctly.
    Collapsing whitespace once here — before any extractor sees a line — is
    the single general fix for that, done exactly where `_lab_row` already
    did its own local version of the same thing. `document.ocr_text` itself
    (what the patient sees) is never touched; this only affects the copy
    used for parsing.
    """
    return [_WS.sub(" ", line).strip() for line in text.splitlines() if line.strip()]


# ── Document date (all types) ────────────────────────────────────────────
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
_MON = r"(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(?P<d>\d{1,2})[/.\-](?P<m>\d{1,2})[/.\-](?P<y>\d{4})\b"), "dmy"),
    (
        re.compile(
            rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?[\s\-]+{_MON}[\s\-,]+(?P<y>\d{{4}})\b",
            re.IGNORECASE,
        ),
        "dMy",
    ),
    (
        re.compile(
            rf"\b{_MON}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<y>\d{{4}})\b", re.IGNORECASE
        ),
        "Mdy",
    ),
    # A day and month OCR'd as one fused run with no separator between them
    # ("1508-2026" for a document that printed "15-08-2026" — the thin "-"
    # between day and month is exactly the mark handwriting OCR drops first,
    # while the wider "-" before the year survives). Far more ambiguous than
    # the others (many unrelated 4-digit-plus-year shapes exist), so —
    # unlike every other pattern here — this one is only ever accepted right
    # after an explicit date label, never taken on its own (enforced below).
    (re.compile(r"\b(?P<d>\d{2})(?P<m>\d{2})[/.\-](?P<y>\d{4})\b"), "ddmm_fused"),
)
_DATE_HINT = re.compile(
    r"(date|dated|issued?|collected|reported|report|given|administered|vaccinat\w*|discharg\w*)"
    r"\W*$",
    re.IGNORECASE,
)
_BIRTH_HINT = re.compile(r"(dob|d\.o\.b|birth|born)\W*$", re.IGNORECASE)
# A labelled date that is some OTHER date than the document's own: when the sample was taken or
# arrived, when the next dose / visit is due. "Sample Collected: 10-08-2026 ... Report Date:
# 12-08-2026" is a report dated the 12th, and the earlier of the two labelled dates is the wrong one.
_OTHER_DATE_HINT = re.compile(
    r"(collect\w*|received?|sample\w*|specimen|next|due|expir\w*|valid\w*)\W*(?:date|on|at)?\W*$",
    re.IGNORECASE,
)


def _label_score(before: str) -> int:
    """How strongly the text just before a date says it is the document's own date:
    2 = labelled as the document's (report / issued / date / vaccination ...),
    1 = labelled, but as another date (collected, received, next due), 0 = unlabelled."""
    if not _DATE_HINT.search(before):
        return 0
    return 1 if _OTHER_DATE_HINT.search(before) else 2


# A "Report Date" label left WITHOUT its value on the label's own line: in a two-column header
# ("Patient ID : CD-45872   Report Date" beside "Lab No.") the reader takes the left column whole and
# the right column's values much later, each on a line of its own. The value is then the first
# line after the label that is NOTHING but a date (and a time): never a line with any other word
# on it, and only when the label itself was left dangling.
_DANGLING_REPORT_DATE = re.compile(r"\b(?:report(?:ed)?|issue)\s*date\s*[:\-]?\s*$", re.IGNORECASE)
_LONE_DATE_LINE = re.compile(
    r"^\s*(?P<d>\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})(?:\s+\d{1,2}:\d{2}(?:\s*[AaPp][Mm])?)?\s*$"
)


def find_detached_report_date(lines: list[str]) -> tuple[int, int, int] | None:
    """`(line index, start, end)` of the date standing alone on its own line after a dangling
    "Report Date" label (see above), or `None`."""
    dangling = False
    for index, line in enumerate(lines):
        if dangling:
            if m := _LONE_DATE_LINE.match(line):
                return index, m.start("d"), m.end("d")
        elif _DANGLING_REPORT_DATE.search(line.rstrip()):
            dangling = True
    return None


def find_document_date(text: str, today: date | None = None) -> date | None:
    """The most plausible date the document itself states (day-first, as is
    normal in Pakistan). Dates in the future, before 1990, or that follow a
    'date of birth' label are ignored; a date labelled as the document's own
    (report / issued / vaccination date...) wins over one labelled as another
    date (collected / received / next due), which wins over an unlabelled one."""
    today = today or dbtypes.today_pk()
    best: tuple[int, int, date] | None = None  # (score, -position, date)
    # where a "Report Date" label's detached value starts, if the report has one
    detached_at: int | None = None
    if found := find_detached_report_date(text.splitlines(keepends=True)):
        index, start, _end = found
        detached_at = sum(len(line) for line in text.splitlines(keepends=True)[:index]) + start
    for pattern, kind in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            groups = m.groupdict()
            try:
                month = int(groups["m"]) if "m" in groups else _MONTHS[groups["mon"][:3].lower()]
                candidate = date(int(groups["y"]), month, int(groups["d"]))
            except (ValueError, KeyError):
                continue
            if candidate > today or candidate.year < 1990:
                continue
            before = text[max(0, m.start() - 24) : m.start()]
            if _BIRTH_HINT.search(before):
                continue
            score = 2 if m.start() == detached_at else _label_score(before)
            if kind == "ddmm_fused" and score == 0:
                continue  # too ambiguous on its own; needs an explicit label
            key = (score, -m.start(), candidate)
            if best is None or key[:2] > best[:2]:
                best = key
    return best[2] if best else None


# ── Prescription ─────────────────────────────────────────────────────────
# `_FORM` lives in the shared-helpers section above (`_LIST_MARKER` needs it).
# `[mrna][gq]` catches "mg" read as "rg"/"ng"/"ag" (the leading "m" lost or
# swapped) or as "mq" (the "g" misread as its round-bowl look-alike "q") — two
# independent corruptions seen on real scans, on either side of the same
# 2-letter unit. No real unit or word otherwise matches "<digits> <2 letters
# from this class>". The OCR'd text itself is never rewritten (still stored
# and shown exactly as read) — this only stops a genuine dosage from making
# the whole line unrecognisable as a medicine.
# The unit alternation is shared by both halves of a compound strength
# ("600 mg / 400 IU", a combination drug) — the second value is never
# restricted to a narrower unit list than the first; nothing about a
# combination product implies its second ingredient is measured in mg/ml
# specifically.
_STRENGTH_UNIT = r"(?:mg|mcg|µg|g|ml|iu|units?|%|[mrna][gq])"
_STRENGTH = re.compile(
    rf"\b\d+(?:\.\d+)?\s?{_STRENGTH_UNIT}(?:\s?/\s?\d+(?:\.\d+)?\s?{_STRENGTH_UNIT})?",
    re.IGNORECASE,
)
_DOSE_UNIT = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:tab(?:let)?s?|cap(?:sule)?s?|tsp|teaspoons?|tbsp|puffs?|drops?|sachets?)\b",
    re.IGNORECASE,
)
# The unit a frequency phrase names — "daily"/"day" was the only one
# recognised until a real prescription surfaced "Once weekly" (a Vitamin D3
# / biologic / weekly-injection dosing schedule) reading back with an empty
# frequency: none of the alternatives below it named "weekly" or "monthly"
# at all, even though the phrase shape ("once/twice X", "N times a X", bare
# "X") is identical to the daily case. Generalised here rather than only
# patched for "weekly", since "monthly" dosing (e.g. some injectables) is
# exactly the same gap.
_FREQ_UNIT = r"(?:daily|day|weekly|week|monthly|month)"
# "N times [a/per]? UNIT" — a real bug: "three times daily" used to read
# back as just "daily", because the ONLY multi-word-count pattern required
# a DIGIT ("\d+") and a MANDATORY "a"/"per" ("3 times a day"), so a spelled-
# out count ("three") with no "a"/"per" between "times" and the unit
# ("three times daily") matched NOTHING at its own position — the scan
# just skipped over it and picked up the bare "daily" alternative later,
# silently discarding the "three times" part. `_COUNT_WORD` accepts both
# digits and spelled-out small counts; "a"/"per" is now optional. Ordered
# before the "once/twice/thrice" alternative so it can never be pre-empted:
# regex alternation in Python is first-match-at-a-position, not automatic
# longest-match, so the alternative that actually captures the WHOLE
# phrase must be the one a scan reaches first at "three"'s own position —
# which happens automatically here since nothing else matches there at all
# today, but keeping the more specific pattern first is still the safer
# convention if that ever changes.
_COUNT_WORD = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
_FREQUENCY = re.compile(
    r"\b\d\s?[-+]\s?\d\s?[-+]\s?\d(?:\s?[-+]\s?\d)?\b"
    rf"|\b{_COUNT_WORD}\s+times?\s+(?:a\s+|per\s+)?{_FREQ_UNIT}\b"
    rf"|\b(?:once|twice|thrice)\s+(?:a\s+|per\s+)?{_FREQ_UNIT}\b"
    r"|\b(?:every|q)\s?\d+\s?(?:hours?|hrs?|h)\b"
    r"|\b(?:od|bd|bid|tds|tid|qid|qds|hs|sos|prn|stat)\b"
    r"|\b(?:at\s+)?(?:bed\s?time|night)\b"
    r"|\b(?:daily|weekly|monthly)\b",
    re.IGNORECASE,
)
_DURATION = re.compile(
    r"(?:\bfor\s+|\bx\s*)\d+\s?(?:days?|d|weeks?|wks?|months?)\b|\b\d+\s?(?:days?|weeks?|wks?|months?)\b",
    re.IGNORECASE,
)
_NOT_A_MEDICINE = re.compile(
    r"^(?:dr|doctor|patient|name|age|sex|gender|date|address|phone|tel|mobile|cnic|"
    r"diagnosis|dx|advice|follow|next|signature|clinic|hospital|reg|pmdc|bp|pulse|temp|"
    r"weight|wt|height|rx|℞|note|notes|review|investigations?)\b",
    re.IGNORECASE,
)
_TABLE_HEADER = re.compile(r"\b(?:medicine|drug|medication)\b", re.IGNORECASE)
# The shape a garbled strength/quantity leaves behind, in two forms: a
# STANDALONE digit token (preceded by whitespace or the start of the name —
# never fused onto a letter, or it could be part of the name itself, like
# the "3" in "Vitamin D3") immediately followed by a comma and 2+ more
# digits ("Complex 2,500." for a stray leading digit before a corrupted
# strength); or, failing that, any run of 2+ digits immediately followed by
# a comma or period ("D3,000." for "D3 1000 IU", the leading digit of
# "1000" lost and the gap OCR'd as a comma — here the "3" IS fused to a
# letter, so only the "000." after the comma is cut away). Only used as a
# last-resort name cut point for an orphaned-marker block (see
# `_looks_like_orphaned_marker`), never as a general "digits mean stop".
_GARBLED_NUMBER = re.compile(r"(?<!\S)\d+,\d{2,}[,.]?|\d{2,}[,.]")


# A form word within a short leading window — up to 6 arbitrary characters
# of noise, then the word, anchored at the very start of the line. This is
# deliberately NOT a plain "anywhere in the line" search: a form word can
# also appear much later, describing the form in parentheses AFTER the
# medicine name ("Ibuprofen 400mg (Tab)") — matching that would wrongly cut
# the name down to nothing. Limiting the search to a short leading window
# still finds the form word behind a garbled list marker ("ile Tab.",
# "2 Tab.") without ever reaching past the medicine name itself.
#
# Two alternatives, for the same reason `_LIST_MARKER` needs two: when the
# leading noise is itself a bare "<digit> " (the shape of a real "2 tablets"
# dosage instruction), the form word must ALSO be followed by a period to
# count — that period is what a corrupted list marker ("2 Tab.") still has
# and a genuine quantity instruction ("2 tablets to be taken") never does.
# Any OTHER leading noise (letters, or none at all, as in "Cap Atorvastatin"
# with no period) carries no such risk, so no period is required there.
_FORM_NEAR_START = re.compile(
    rf"^(?:\d{{1,2}}\s+{_FORM}\.|(?!\d{{1,2}}\s)[^\n]{{0,6}}?\b{_FORM}\b\.?)",
    re.IGNORECASE,
)
# Handwritten instructions routinely spread across 2-3 lines of their own
# ("Take 1 tablet every 6 hours as needed" / "for fever or pain") on top of
# the medicine's own name+strength line — a cap of 2 leaves no room for that
# whole instruction before the next medicine's stronger signal (marker/form
# word/name+strength) correctly closes the block anyway, so raising it does
# not risk swallowing a genuine next entry.
_MAX_CONTINUATION_LINES = 3
_MAX_CONTINUATION_LEN = 80


def _looks_like_new_medicine(line: str) -> bool:
    """A strong, self-contained signal that a line BEGINS a medicine entry:
    its own list marker, a dosage-form word near its start, or — when
    neither is present at all — a strength/dose pattern with a plausible
    name in front of it on the SAME line. That last check is what a
    handwritten prescription needs when its form word sits in parentheses
    AFTER the name instead of before it, with no numbering at all
    ("Ibuprofen 400mg (Tab)"): there is no marker and no leading form word,
    so only the presence of real letters before the strength distinguishes
    it from a bare wrapped-dosage fragment. A bare fragment like "500 mg
    (after meals)" has nothing before its strength match, so it is never
    mistaken for a new medicine and can still be absorbed as a
    continuation of the block already open."""
    if _LIST_MARKER.match(line):
        return True
    form_m = _FORM_NEAR_START.match(line)
    if form_m:
        # The form word alone is not enough to start a new block: "(Tab)
        # after meals" on its own continuation line matches this exactly as
        # "1. Tab. Metformin" does, but only the latter actually names a NEW
        # medicine. What follows the form word must itself look like the
        # start of a name (a capitalised word) — a lowercase administration
        # phrase ("after meals", "at night", "in the morning") means this is
        # a dosage-form annotation for whatever medicine's block is already
        # open, not the start of another one.
        after = line[form_m.end() :].lstrip(" .():-")
        return bool(after[:1].isalpha() and after[:1].isupper())
    strength_m = _STRENGTH.search(line)
    if strength_m and re.search(r"[A-Za-z]{2}", line[: strength_m.start()]):
        return True
    return _looks_like_orphaned_marker(line)


_ORPHAN_MARKER = re.compile(r"^\d{1,2}\s+[A-Z][a-z]")


def _looks_like_orphaned_marker(line: str) -> bool:
    """A line shaped like a numbered list marker that lost its punctuation (a
    bare 1-2 digit number immediately followed by a capitalised word — the
    same corruption `_LIST_MARKER`'s "2 Tab." alternative already recovers
    when a form word survives) whose own strength/dosage was OCR'd beyond any
    pattern `_looks_like_new_medicine`'s other checks can recognise ("3
    Vitamin D3,000. IN." for "3. Vitamin D3 1000 IU"). It carries none of
    `_STRENGTH`, `_DOSE_UNIT`, `_FREQUENCY` or `_DURATION` — the same
    invariant that already rules out a genuine continuation line starting
    with a digit ("1 Tablet before meals", "6 hours"), since a real
    continuation always supplies at least one of those. That means it is
    never a continuation of whatever block is open, but the marker shape
    still makes it a plausible NEW medicine whose name (if not its dosage)
    may still be legible — treating it as one, rather than silently folding
    it into a neighbouring block, stops it corrupting that block's data and
    lets its own continuation lines (which may still carry a real
    frequency/duration) attach to it instead of being dropped for having
    nowhere to go."""
    if not _ORPHAN_MARKER.match(line):
        return False
    return not (
        _STRENGTH.search(line)
        or _DOSE_UNIT.search(line)
        or _FREQUENCY.search(line)
        or _DURATION.search(line)
    )


# General advice that follows a prescription's medicine list WITHOUT a
# heading ("Drink plenty of fluids", "Avoid oily food", "Review after 3
# days"). It is short prose, so the continuation rule below would otherwise
# glue it onto the LAST medicine's block — where a stray "for 3 days" in it
# became that medicine's duration. Deliberately limited to verbs that are
# unambiguously general advice: "Take"/"Use"/"Apply" are NOT here because
# they genuinely begin a medicine's own instruction ("Apply twice daily").
_ADVICE_PROSE_START = re.compile(
    r"^\W*(?:drink|avoid|rest|eat|gargle|complete|continue|keep|plenty|increase|reduce|"
    r"don'?t|do\s+not|maintain|sleep|walk|exercise|bed\s+rest|light\s+diet|soft\s+diet|"
    r"steam|wash|stay|limit|come\s+back|return|visit|consult|review|follow[\s-]?up|"
    r"see\s+(?:the\s+)?doctor|next\s+(?:visit|appointment))\b",
    re.IGNORECASE,
)


def looks_like_general_advice(line: str) -> bool:
    """A line that reads as general advice rather than a medicine's own
    instruction — see `_ADVICE_PROSE_START`. Never true for a line that
    itself begins a medicine entry."""
    checked = _LIST_MARKER.sub("", line).strip()
    return bool(_ADVICE_PROSE_START.match(checked)) and not _looks_like_new_medicine(line)


def _prescription_block_indexes(lines: list[str]) -> list[list[int]]:
    """Group raw OCR lines into one block per medicine candidate, as LINE
    INDEXES (see `_prescription_blocks` for the rules). Indexes rather than
    text so other stages can tell which lines of the document are medicine
    lines — `sections.py` needs that to end the advice section where the
    medicines are, and to find advice written after them."""
    blocks: list[list[int]] = []
    current: list[int] | None = None
    for index, line in enumerate(lines):
        checked = _LIST_MARKER.sub("", line).strip()
        if not checked or _NOT_A_MEDICINE.match(checked):
            current = None
            continue
        if _looks_like_new_medicine(line):
            current = [index]
            blocks.append(current)
            continue
        if current is not None and looks_like_general_advice(line):
            current = None  # advice ends the medicine; it is not that medicine's instruction
            continue
        if (
            current is not None
            and len(current) <= _MAX_CONTINUATION_LINES
            and len(line) < _MAX_CONTINUATION_LEN
        ):
            current.append(index)
        else:
            current = None  # nothing open, and this line doesn't start anything either
    return blocks


def _prescription_blocks(lines: list[str]) -> list[list[str]]:
    """Group raw OCR lines into one block per medicine candidate.

    Printed and handwritten prescriptions alike routinely wrap one medicine
    across two physical lines — most often the dosage strength, or an
    instruction, landing on its own line below the medicine name. A
    single-line-only extractor can never recover a medicine whose name and
    strength end up on different lines. A block runs from a line that
    clearly begins an entry (see `_looks_like_new_medicine`) up to, but not
    including, the next such line; a line that is clearly NOT part of a
    medicine (a header/advice/signature/... line, via `_NOT_A_MEDICINE`, or
    general advice prose, via `_ADVICE_PROSE_START`) closes the block
    outright rather than extending it.
    """
    return [[lines[i] for i in block] for block in _prescription_block_indexes(lines)]


def medicine_line_indexes(lines: list[str]) -> set[int]:
    """Indexes into `lines` of every line that belongs to a medicine the
    extractor actually ACCEPTS (a block it would reject as not-a-medicine
    does not count)."""
    found: set[int] = set()
    for block in _prescription_block_indexes(lines):
        if _prescription_row_from_block([lines[i] for i in block]) is not None:
            found.update(block)
    return found


def _prescription_row_from_block(block: list[str]) -> dict[str, str] | None:
    """One medicine from a block of 1+ raw OCR lines, or None."""
    first = block[0]
    cells = [c.strip() for c in first.split("|")] if "|" in first else [first]
    # A stray "|" sometimes lands INSIDE a corrupted list marker itself
    # ("2. | Tab. Atorvastatin..."), pushing all the real content into the
    # next cell. A first cell with no letters at all, once its own list
    # marker is stripped, was never real content — the next cell is used
    # instead, exactly as if the "|" had not been there.
    lead = _LIST_MARKER.sub("", cells[0]).strip()
    if len(cells) > 1 and not re.search(r"[A-Za-z]", lead):
        cells = cells[1:]
    # A numbered marker that kept its digit but lost BOTH its punctuation
    # ("1." -> "1") and any form word ("1 Metformin 500 mg..." for "1.
    # Metformin 500 mg...") is not covered by `_LIST_MARKER` (which requires
    # either the punctuation or a form word right after the digit, to avoid
    # ever mistaking a genuine digit-led drug name like "3TC" — no space
    # before the letters — for a marker). A bare digit followed by WHITESPACE
    # then a capital letter has no such ambiguity: no real drug name is
    # written "1 Metformin" with a space there, so it is always the
    # marker.
    head = _BARE_NUM_MARKER.sub("", _LIST_MARKER.sub("", cells[0]).strip())
    if not head or _NOT_A_MEDICINE.match(head):
        return None
    if len(cells) > 1 and _TABLE_HEADER.search(head) and len(head) < 20:
        return None  # a table's header row

    # The medicine name starts right after the form word, wherever it is —
    # never assumed to be at position 0 — so a garbled marker before it
    # ("ile Tab. Metformin") is never mistaken for part of the name.
    form_m = _FORM_NEAR_START.match(head)
    has_form = bool(form_m)
    name_source = head[form_m.end() :].strip() if form_m else head

    # Dosage/frequency/duration are searched across the WHOLE block — they
    # routinely land on a continuation line, not the line the name is on.
    full_body = " ".join([name_source, *cells[1:], *block[1:]])
    strength = _STRENGTH.search(full_body)
    # `_looks_like_orphaned_marker` is what opened this block in the first
    # place when neither a form word nor a strength was found ANYWHERE on
    # its own line: the marker shape alone is enough to accept it, so a
    # medicine whose strength is unreadable still gets a row for its name
    # rather than being dropped entirely.
    orphaned = _looks_like_orphaned_marker(first)
    if not has_form and not strength and not orphaned:
        return None

    # The medicine NAME itself only ever comes from its own line, never from
    # a continuation line, or "500 mg" from the next line could be read as
    # part of the name instead of being cut away from it.
    cut = len(name_source)
    for pattern in (_STRENGTH, _DOSE_UNIT, _FREQUENCY, _DURATION):
        m = pattern.search(name_source)
        if m:
            cut = min(cut, m.start())
    if cut == len(name_source) and orphaned:
        # None of the field patterns found a cut point — expected, since a
        # block only reaches here when its strength was too garbled for any
        # of them to match at all. Fall back to cutting before the first run
        # of 2+ digits immediately followed by punctuation noise, the shape
        # a garbled number/unit reliably leaves behind ("D3,000." for "D3
        # 1000 IU") — never a digit that is genuinely part of the name
        # itself (a bare "D3" has no such trailing punctuation).
        garbled_m = _GARBLED_NUMBER.search(name_source)
        if garbled_m:
            cut = min(cut, garbled_m.start())
    medicine = _clean(name_source[:cut])
    if len(medicine) < 2 or not re.search(r"[A-Za-z]{2}", medicine):
        return None

    dosage_m = strength or _DOSE_UNIT.search(full_body)
    freq_m = _FREQUENCY.search(full_body)
    dur_m = _DURATION.search(full_body)
    return {
        "medicine": medicine,
        "dosage": _clean(dosage_m.group(0)) if dosage_m else "",
        "frequency": _clean(freq_m.group(0)) if freq_m else "",
        "duration": _clean(re.sub(r"^(?:for|x)\s*", "", dur_m.group(0), flags=re.IGNORECASE))
        if dur_m
        else "",
    }


def _prescription_row(line: str) -> dict[str, str] | None:
    """One medicine from a single line — kept for the boundary tests that
    exercise it directly. `extract_prescription` itself calls
    `_prescription_row_from_block`, so a medicine's fields can span more than
    one physical OCR line."""
    return _prescription_row_from_block([line])


def _prescription_entities_with_sources(text: str) -> tuple[list[dict[str, str]], list[str]]:
    """Same grouping `parse_prescription_entities` does, but also returns
    each entity's own raw block text (the OCR lines it was built from),
    aligned 1:1 by position. `parse_prescription_entities` itself drops the
    second list — its public shape is used directly by many existing
    callers/tests and must not change — but `extract_prescription` needs
    the raw text alongside each row so `normalization.py` can judge
    certainty against what was ACTUALLY on the page (e.g. telling "this
    field is genuinely blank" apart from "something was here but too
    garbled to read" — see `normalization.normalize_prescription_rows`)."""
    entities: list[dict[str, str]] = []
    sources: list[str] = []
    for block in _prescription_blocks(_lines(text)):
        entity = _prescription_row_from_block(block)
        if entity is not None:
            entities.append(entity)
            sources.append(" ".join(block))
        if len(entities) >= MAX_ROWS:
            break
    return entities, sources


def parse_prescription_entities(text: str) -> list[dict[str, str]]:
    """RAW OCR TEXT -> STRUCTURED PRESCRIPTION JSON.

    The one place raw OCR lines are grouped into logical medicine entities
    and turned into the intermediate representation the Review page's fields
    are populated from: `[{"medicine": ..., "dosage": ..., "frequency": ...,
    "duration": ...}, ...]`, one object per identified medicine, any field
    genuinely absent from the OCR text left as `""` rather than guessed at.

    This is deliberately a two-stage pipeline, not a per-line scan: first
    `_prescription_blocks` decides which raw lines belong to the SAME
    medicine (a name/strength line plus however many wrapped instruction
    lines follow it, up to the next medicine's own stronger signal), then
    `_prescription_row_from_block` reads each field out of that whole block
    rather than a single line — so "Metformin 500 mg" / "Take 1 tablet
    twice daily" / "for 30 days" becomes one entity, never three unrelated
    fragments. Printed (Tesseract) and handwritten (TrOCR) OCR both end up
    as plain text by the time it reaches here, so this stage is shared
    unchanged between them — it never knows or asks which engine produced
    `text`, only that it is now a string of lines to structure.
    """
    entities, _ = _prescription_entities_with_sources(text)
    return entities


def extract_prescription(text: str, today: date | None = None) -> Extraction:
    entities, sources = _prescription_entities_with_sources(text)
    # AI Understanding / Normalization: a separate stage that cleans a
    # stray leading OCR-noise symbol off a medicine name ("& Metformin" ->
    # "Metformin") and scores each field's certainty — never re-decides
    # which lines belong to which medicine (that stays entirely
    # structuring's job, above). See `normalization.py`.
    rows, certainty = normalization.normalize_prescription_rows(entities, sources)
    return Extraction(
        rows=rows, document_date=find_document_date(text, today), field_certainty=certainty
    )


# ── Lab report ───────────────────────────────────────────────────────────
# "patient" needs its own `\s*(?:id|name)` alternative, matching how "lab" and
# "ref(erence)" already handle their own compounds below: a bare `patient\b`
# never matches "PatientID" — "t" and "I" are both word characters, so there is
# no boundary between them for `\b` to find — and it would otherwise fall
# through to `_LAB_LINE` and be misread as a lab result (test_name="PatientID").
_NOT_A_LAB_LINE = re.compile(
    r"^(?:patient\s*(?:id|name)|patient|name|age|sex|gender|date|dob|phone|tel|mobile|cnic|address|"
    r"ref(?:erence)?\s*(?:no|#)|sample|specimen|lab\s*(?:no|id)|report|page|collected|"
    r"received|reported|doctor|dr|mr|mrs|ms|id|mrn|invoice|test\s*name|investigation)\b",
    re.IGNORECASE,
)
# A plain number, or one with thousands-commas ("7,200") — shared by the
# result value and the reference range, since OCR puts commas in either.
_COMMA_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_NUM = rf"[<>]?\s?(?:{_COMMA_NUM})"
# "=" joins "-"/"–"/"—" as a range separator: a common OCR misread of a dash
# (observed: "11.5 = 15.5" for a document that printed "11.5 - 15.5").
_REF = rf"(?:[<>≤≥]=?\s?(?:{_COMMA_NUM})|(?:{_COMMA_NUM})\s?[-–—=]\s?(?:{_COMMA_NUM}))"
_REF_PATTERN = re.compile(_REF, re.IGNORECASE)
# A test name is words that start with a letter — never a bare number — so it
# cannot swallow the result that follows it.
_WORD = r"[A-Za-z(][A-Za-z0-9()/%.,'&+\-]*"
_UNIT = r"(?:%|/?[A-Za-z][A-Za-z0-9%µ^.*\-]*(?:/[A-Za-z0-9%µ^.*\-]+)*)"
# Everything after the unit is captured as `rest` rather than required to
# parse as a reference range: a range the OCR mangled beyond recognition (a
# separator this doesn't know, or plain garbage) must not throw away the test
# name/result/unit that WERE read correctly — `_lab_row` below searches `rest`
# for a range best-effort and leaves `reference_range` blank if it can't find
# one, rather than discarding the whole row.
_LAB_LINE = re.compile(
    rf"""^(?P<name>{_WORD}(?:\ {_WORD})*)\s*[:|]?\s+
         (?P<value>{_NUM})\s*
         (?P<unit>{_UNIT})?
         (?P<rest>.*)$""",
    re.IGNORECASE | re.VERBOSE,
)
# OCR noise standing in for an icon or logo mark (observed: a phone-icon
# glyph read as "eee") never looks like this: a real clinical unit is never
# the same letter repeated 3+ times.
_NOISE_UNIT = re.compile(r"^([A-Za-z])\1{2,}$")
# A genuine (even badly garbled) reference range or trailing flag never
# leaves more than one real word behind once it — and any range this DID
# manage to parse — are accounted for; two or more is what ordinary prose
# looks like ("visits scheduled today please").
_WORDLIKE = re.compile(r"[A-Za-z]{2,}")


def _lab_row(line: str) -> dict[str, str] | None:
    line = _LIST_MARKER.sub("", line).strip()
    if not line or _NOT_A_LAB_LINE.match(line):
        return None
    # A pipe-separated table row ("Glucose | 142 | mg/dL | 70-100") reads the
    # same with the pipes as spaces.
    m = _LAB_LINE.match(_WS.sub(" ", line.replace("|", " ")).strip())
    if m is None:
        return None
    name = _clean(m.group("name"))
    if len(re.findall(r"[A-Za-z]", name)) < 2:
        return None
    unit = m.group("unit") or ""
    if _NOISE_UNIT.match(unit):
        return None  # e.g. "eee": not a unit, so this was never a result line
    # A bare word after the number that is really a flag, not a unit.
    if unit.lower() in {"h", "l", "high", "low", "normal", "abnormal"}:
        unit = ""
    rest = (m.group("rest") or "").strip()
    ref_m = _REF_PATTERN.search(rest)
    if not unit and ref_m is None and rest:
        # No unit, no range anywhere in what follows, yet something is still
        # there ("Report Date: 28/07/2026" trailing after a bare "28"): the
        # "value" was never really a result, just a number inside other text.
        return None
    remainder = rest[: ref_m.start()] + rest[ref_m.end() :] if ref_m else rest
    if len(_WORDLIKE.findall(remainder)) >= 2:
        # Whatever is left after the range (or the whole tail, if no range
        # was found) reads as ordinary sentences, not OCR noise around a
        # range or a single trailing flag word — this was never a result.
        return None
    ref = _clean(re.sub(r"\s*[-–—=]\s*", "-", ref_m.group(0))) if ref_m else ""
    return {
        "test_name": name,
        "result": _clean(m.group("value").replace(" ", "")),
        "unit": _clean(unit),
        "reference_range": ref,
    }


def _range_only_continuation(line: str) -> re.Match[str] | None:
    """A short line that is plausibly nothing but a reference range wrapped
    onto its own line — never a real lab line in its own right (it must not
    itself parse as one), never a header, and short enough that it cannot be
    an unrelated sentence that happens to contain two numbers."""
    stripped = _LIST_MARKER.sub("", line).strip()
    if not stripped or _NOT_A_LAB_LINE.match(stripped) or len(stripped) >= 40:
        return None
    if _lab_row(line) is not None:
        return None  # a full result of its own, not a wrapped range
    return _REF_PATTERN.search(stripped)


def extract_lab(text: str, today: date | None = None) -> Extraction:
    lines = _lines(text)
    rows: list[dict[str, str]] = []
    i = 0
    while i < len(lines):
        row = _lab_row(lines[i])
        if row is not None:
            # The range is the field most likely to wrap onto its own line —
            # a genuine result was already kept even without one, so this
            # only ever fills a gap, never overwrites a range already found.
            if not row["reference_range"] and i + 1 < len(lines):
                cont_m = _range_only_continuation(lines[i + 1])
                if cont_m is not None:
                    row["reference_range"] = _clean(re.sub(r"\s*[-–—=]\s*", "-", cont_m.group(0)))
                    i += 1
            rows.append(row)
        if len(rows) >= MAX_ROWS:
            break
        i += 1
    return Extraction(rows=rows, document_date=find_document_date(text, today))


# ── Other medical document ───────────────────────────────────────────────
# A dose a medicine can have: a number and a real dose unit — NOT a percentage and NOT a
# per-volume concentration, which is what a lab value looks like ("7.2 %", "142 mg/dL").
_MEDICINE_DOSE = re.compile(
    r"\b\d[\d.,]*\s*(?:mg|mcg|µg|ug|gm|g|ml|iu|units?)\b(?!\s*/\s*(?:d?l|min|hr|h)\b)",
    re.IGNORECASE,
)


# A laboratory unit: a quantity PER litre / decilitre ("142 mg/dL", "5.2 mmol/L", "40 U/L"). The
# prescription reader keeps only "142 mg" of such a value, so the ORIGINAL line has to be checked.
_LAB_UNIT = re.compile(r"\d\s*(?:[mµu]?(?:g|mol|iu|u|eq))\s*/\s*(?:d?l)\b", re.IGNORECASE)


def _named_on_a_lab_line(name: str, text: str) -> bool:
    needle = name.casefold()
    return any(needle in line.casefold() and _LAB_UNIT.search(line) for line in text.splitlines())


def extract_other(text: str, today: date | None = None) -> Extraction:
    """An "other" medical document (a discharge summary, a referral, a clinic letter) is read
    for medicines with the prescription reader, so that the medicines it does list reach the
    patient for review — but held to a STRICTER standard. A prescription is made of medicine
    lines; this is arbitrary text, in which "HbA1c 7.2 %" or "Dr. Ahmed" can look like one. So
    a row is kept only if its dosage is a real medicine dose (`_MEDICINE_DOSE`). A medicine
    named without a dose is therefore not proposed — the patient can add it — which is the
    right way round for a document that is not a prescription: nothing is invented, and a
    document with no medicines yields no rows and can still be saved."""
    found = extract_prescription(text, today)
    keep = [
        i
        for i, row in enumerate(found.rows)
        if _MEDICINE_DOSE.search(row.get("dosage", ""))
        and not _named_on_a_lab_line(row.get("medicine", ""), text)
    ]
    return Extraction(
        rows=[found.rows[i] for i in keep],
        document_date=found.document_date,
        field_certainty=(
            [found.field_certainty[i] for i in keep] if found.field_certainty is not None else None
        ),
    )


# ── Vaccination record ───────────────────────────────────────────────────
def extract_text_only(text: str, today: date | None = None) -> Extraction:
    """No detailed schema exists for these types, so nothing clinical is
    invented: the OCR text is preserved on the document and the only proposal
    is the date the document states."""
    return Extraction(rows=[], document_date=find_document_date(text, today))


Extractor = Callable[[str, date | None], Extraction]

EXTRACTORS: dict[str, Extractor] = {
    DOC_TYPE_RX: extract_prescription,
    DOC_TYPE_LAB: extract_lab,
    DOC_TYPE_VACCINE: extract_text_only,
    DOC_TYPE_OTHER: extract_other,
}


def get_extractor(doc_type: str) -> Extractor:
    return EXTRACTORS[doc_type]
