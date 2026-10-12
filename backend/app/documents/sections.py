"""Turning a finalized document's OCR text into a labelled, sectioned
presentation for the View/Details page — display only.

This is deliberately separate from `extractors.py`: the clinical fields
(medicine/dosage/frequency/duration, test/result/unit/range) are exactly
what they always were, read from the confirmed `OcrExtraction` rows. This
module only gives a name to the OTHER lines of the same OCR text — the
facility, the patient, the prescriber, the document's own dates, and any
closing notes, vital signs and vaccine doses — the parts a raw-text dump left unstructured. Nothing here
is a clinical field, nothing here changes what gets extracted or stored,
and a value this module cannot confidently label is simply left out, never
guessed (PRD's "never invent").

Extraction here is the same deterministic, regex-over-text approach as
`extractors.py`, for the same reason: OCR text is attacker-controlled input,
and every value is a display proposal, never trusted as fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..db.models import DOC_TYPE_LAB, DOC_TYPE_OTHER, DOC_TYPE_RX, DOC_TYPE_VACCINE
from . import extractors, vaccines, vitals

_WS = re.compile(r"\s+")


def _lines(text: str) -> list[str]:
    return [_WS.sub(" ", line).strip() for line in text.splitlines() if line.strip()]


def _clean(value: str) -> str:
    return _WS.sub(" ", value).strip(" \t.,-:;|")


@dataclass
class DocumentSections:
    """Ordered, label -> value pairs per section; a section with nothing
    found in it is simply empty, never padded with placeholders. `notes` is
    a list because a prescription's or report's closing remarks are
    naturally a list of short items, not a single value.

    Each tuple is `(key, value, unreadable)`: `unreadable` is True only for
    a field that WAS found but had to be dropped by `_Collector.add`'s
    length cap (see `_MAX_VALUE_LEN`) — distinct from a field that was
    simply never present at all, which never gets an entry here. `value`
    is always `""` when `unreadable` is True: never a guess, never display
    text (this module stays locale-agnostic; see `_Collector.add`)."""

    facility: list[tuple[str, str, bool]] = field(default_factory=list)
    patient: list[tuple[str, str, bool]] = field(default_factory=list)
    provider: list[tuple[str, str, bool]] = field(default_factory=list)
    report: list[tuple[str, str, bool]] = field(default_factory=list)
    # Vital signs (prescription / other documents) — same tuple shape.
    vitals: list[tuple[str, str, bool]] = field(default_factory=list)
    # Vaccine doses (vaccination records): one dict per dose, keys from
    # `vaccines.ENTRY_KEYS`. Display only — there is no table for them.
    entries: list[dict[str, str]] = field(default_factory=list)
    # Allergies the document names — one dict per allergy, keys `allergy_substance` /
    # `allergy_reaction` / `allergy_severity`. Never a displayed section: they are proposed
    # as reviewable rows (`proposals.py`) and, once the patient confirms, join their profile.
    allergies: list[dict[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # The headed passages of an "other" medical document (findings, conclusion, procedure,
    # hospital course...) — `(key, lines)` in the order they appear, keys from `TOPIC_KEYS`.
    # Only the lines under a heading the document itself printed; display only.
    topics: list[tuple[str, list[str]]] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.facility
            or self.patient
            or self.provider
            or self.report
            or self.vitals
            or self.entries
            or self.notes
            or self.topics
        )


# ── Label vocabulary ─────────────────────────────────────────────────────
# Real OCR routinely puts more than one labelled field on the SAME physical
# line ("Patient: Ayesha Raza   Age: 32", "Chughtai Lab   Report Date:
# 28/07/2026") — every pattern below is unanchored (`.search()`, not
# `.match()`) so a label is found wherever it sits, and every captured value
# is passed through `_cut_at_next_label`, which trims it the instant
# ANOTHER known label starts — otherwise a value would swallow the rest of
# the line, including a different field entirely.
def _alt(*words: str) -> str:
    return r"(?:" + "|".join(words) + r")"


_LABEL_WORDS = _alt(
    "patient\\s*name",
    "patient\\s*id",
    "patient\\s*no",
    # A bare "patient" fallback, AFTER the more specific compounds above
    # (alternation tries them in order, so a degraded "Patient ID :" still
    # matches the specific "patient\\s*id" alternative when it can — this
    # is only reached when the qualifier word itself didn't survive).
    # Widens the set of degraded labels `_cut_at_next_label` can still
    # recognise as SOME field starting, even when it can no longer tell
    # which one — better to stop a runaway value one word early than not
    # stop it at all.
    # "Patient ID :" read as "Patient (0 -" — the qualifier is junk but a
    # label is clearly starting, which is all a value boundary needs.
    "patient\\s*[(\\[]?\\w{0,2}",
    "mrn",
    "age\\s*/\\s*gender",
    "age",
    "gender",
    "sex",
    "address",
    "ph",
    "phone",
    "tel",
    "mobile",
    "contact",
    "email",
    "doctor",
    "referred\\s*by",
    "ref\\.?\\s*by",
    "lab\\s*no",
    "consultant",
    "specialization",
    "speciality",
    "specialisation",
    "department",
    "date\\s*of\\s*birth",
    "dob",
    "report\\s*date",
    "prescription\\s*date",
    "date",
    "sample\\s*collection\\s*date",
    "collection\\s*date",
    "sample\\s*date",
    "specimen\\s*date",
    "collected",
    "received\\s*date",
    "received",
    "diagnosis",
    "dx",
    "impression",
    "chief\\s*complaints?",
    "presenting\\s*(?:complaints?|symptoms?)",
    "complaints?",
    "c\\s*/\\s*o",
    "blood\\s*pressure",
    "bp",
    "pulse",
    "temp(?:erature)?",
    "spo2",
    "weight",
    "height",
    "bmi",
    "allerg(?:y|ies)",
    "notes?",
    "advice",
    "instructions?",
    "remarks?",
    "comments?",
    "interpretation",
)
_ANY_LABEL_START = re.compile(rf"\b{_LABEL_WORDS}\.?\s*[:.\-|;=+]", re.IGNORECASE)

# Shapes that can only be a particular field's VALUE. A label whose separator
# OCR dropped completely ("Date 10-08-2026", "Patient ID 2026-7890", "Doctor
# Dr. Neha Kapoor") is still a label when one of these follows it — and only
# then: a bare label word followed by ordinary words ("Date of review",
# "Patient advised ...") is never read as a label.
_DATE_SHAPE = r"\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b"
_ID_SHAPE = r"[A-Za-z]{0,3}-?\d{3,}"
_LABEL_NO_SEP = re.compile(
    rf"\b(?:date|patient\s*id|patient\s*no|mrn|doctor|consultant)\s+"
    rf"(?=(?:{_DATE_SHAPE}|{_ID_SHAPE}|dr\b))",
    re.IGNORECASE,
)


# A label at the very END of a value, with nothing after it: the next field's name,
# whose own value the scan put in another column ("Patient ID : CD-45872 Report Date").
_TRAILING_LABEL = re.compile(
    r"\s+(?:(?:report|collection|received|sample|specimen)\s*date|lab\s*no\.?)\s*$",
    re.IGNORECASE,
)


def _cut_at_next_label(line: str, value_start: int) -> str:
    """Everything from `value_start` to the line's end, or up to the next
    recognised label if one starts before the line does — the general fix
    for two labelled fields sharing one physical OCR line."""
    return _TRAILING_LABEL.sub("", _cut_before_label(line, value_start))


def _cut_before_label(line: str, value_start: int) -> str:
    cuts = [
        m.start()
        for m in (
            _ANY_LABEL_START.search(line, value_start),
            _LABEL_NO_SEP.search(line, value_start),
        )
        if m
    ]
    return line[value_start : min(cuts)] if cuts else line[value_start:]


# The separator after a label tolerates a period as well as a colon/dash —
# OCR routinely misreads ":" as "." (a very common confusion, especially in
# handwriting: "patientName ." for "Patient Name:"), and without this a
# label with a damaged separator is invisible to every pattern below.
# "=" too: a colon read as "=:" ("Patient Name =: Priya") is a real, observed
# artifact; the stray ":" left in the value is trimmed by `_clean`.
_SEP = r"[:.\-|;=+]"
# Date and clinical labels keep the strict separator: "|" is also a table
# column divider, and "Date | Time | Result" is a header, not a date.
_SEP_STRICT = r"[:.\-+]"
_PATIENT_NAME = re.compile(rf"\bpatient\s*name\s*{_SEP}\s*", re.IGNORECASE)
_PATIENT_LABEL = re.compile(rf"\bpatient\s*{_SEP}\s*", re.IGNORECASE)
_PATIENT_ID = re.compile(
    rf"\bpatient\s*(?:id|no)\.?\s*(?:{_SEP}\s*|(?={_ID_SHAPE}))"
    rf"|\bmrn\.?\s*(?:{_SEP}\s*|(?={_ID_SHAPE}))",
    re.IGNORECASE,
)
# "/" is a common OCR misread as "(" (similar shape) between two short
# labels on the same line — the same tolerance already given to other
# corrupted separators elsewhere in this pipeline (e.g. a reference range's
# dash misread as "=").
_AGE_GENDER = re.compile(rf"\bage\s*[/(]\s*gender\s*(?:{_SEP}\s*|(?=\d))", re.IGNORECASE)
# The combined VALUE ("32 years / Female") uses its own separator, also
# routinely misread by OCR — "!" for "/" is a real, observed artifact (the
# two glyphs are a similar height/shape in a low-quality scan).
_AGE_GENDER_VALUE_SEP = re.compile(r"[/!]")
_AGE = re.compile(rf"\bage\s*(?:{_SEP}\s*|(?=\d{{1,3}}\b))", re.IGNORECASE)
_DOB = re.compile(rf"\b(?:date\s*of\s*birth|d\.?o\.?b\.?)\s*{_SEP_STRICT}\s*", re.IGNORECASE)
# A bare age is displayed as just the number ("32", not "32 years" / "32yrs")
# — never invented: if no number is present at all, the cleaned raw text is
# kept as-is rather than discarded.
_AGE_DIGITS = re.compile(r"\d{1,3}")
_GENDER = re.compile(rf"\b(?:gender|sex)\s*{_SEP}\s*", re.IGNORECASE)
_ADDRESS = re.compile(rf"\baddress\s*{_SEP}\s*", re.IGNORECASE)
_PHONE = re.compile(
    rf"\b(?:ph|phone|tel|mobile|contact)(?:\s*(?:no|number)\b)?\.?\s*{_SEP}\s*", re.IGNORECASE
)
_EMAIL_LABELLED = re.compile(rf"\bemail\s*{_SEP}\s*", re.IGNORECASE)
_EMAIL_BARE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_DOCTOR_LABELLED = re.compile(rf"\b(?:doctor|consultant)\s*(?:{_SEP}\s*|(?=dr\b))", re.IGNORECASE)
# "Ref. By : Dr. Sana Malik" names the doctor who SENT the patient — not the one who wrote
# or signed this document, and never to be mixed with the signer's qualification.
_REFERRED_BY = re.compile(
    rf"\b(?:referred\s*by|ref(?:erred)?\.?\s*by|referring\s*(?:doctor|physician))\s*{_SEP}\s*",
    re.IGNORECASE,
)
# Case-insensitive only on "dr" itself (`(?i:...)` scopes the flag) — the
# name that follows must still start with a capital letter, or a stray
# lowercase word after a garbled "dr" fragment would be mistaken for one.
# The leading `(?:...)*` absorbs any number of REPEATED "Dr."/"Dr" tokens
# (a real, observed OCR artifact: "Dr. Dr. Sara Ahmed" for "Dr. Sara Ahmed")
# before the captured group below, so the value is always just the last,
# genuine "Dr. <Name>" — never "Dr. Dr".
_DOCTOR_SIGNATURE = re.compile(
    r"(?:\b(?i:dr)\.?\s+)*(\b(?i:dr)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)"
)
_QUALIFICATION = re.compile(
    r"\b(?:MBBS|MD|MS|BDS|DDS|PharmD|PhD|FCPS|MRCP|MRCS|FRCS|FRCP|DGO|DCH|DNB|BSc|MSc|RN)\b"
    r"(?:[.,\s]*\b(?:MBBS|MD|MS|BDS|DDS|PharmD|PhD|FCPS|MRCP|MRCS|FRCS|FRCP|DGO|DCH|DNB|BSc|MSc|RN)\b)*"
)
_SPECIALIZATION_PAREN = re.compile(r"\(([^)]+)\)")
_SPECIALIZATION_LABELLED = re.compile(
    rf"\b(?:specialization|speciality|specialisation|department)\s*{_SEP}\s*", re.IGNORECASE
)
_REPORT_DATE = re.compile(
    rf"\b(?:report|prescription)\s*date\s*(?:{_SEP_STRICT}\s*|(?={_DATE_SHAPE}))", re.IGNORECASE
)
# A bare "Date:" — no "Report"/"Prescription" prefix — is at least as common
# in practice as the compound label above, and is used only as a fallback
# when that one isn't found. It cannot mistake "Date of Birth:"/"DOB:" for
# the document's own date: "of Birth" sits between "date" and the separator,
# which `\s*` (whitespace only) does not cross, so the pattern simply never
# matches there — no explicit DOB exclusion needed.
_DATE_BARE = re.compile(rf"\bdate\s*(?:{_SEP_STRICT}\s*|(?={_DATE_SHAPE}))", re.IGNORECASE)
_NOT_THE_DOCUMENT_DATE = ("collection", "received", "sample", "specimen", "birth", "due", "expiry")


def _bare_date_match(line: str) -> re.Match[str] | None:
    """The bare "Date:" label — unless the word before it says it is some OTHER
    date ("Sample Collection Date:", "Received Date:")."""
    for m in _DATE_BARE.finditer(line):
        if not line[: m.start()].rstrip().lower().endswith(_NOT_THE_DOCUMENT_DATE):
            return m
    return None


_COLLECTION_DATE = re.compile(
    rf"\b(?:collection\s*date|sample\s*date|specimen\s*date|collected)\s*{_SEP_STRICT}\s*",
    re.IGNORECASE,
)
_RECEIVED_DATE = re.compile(rf"\breceived(?:\s*date)?\s*{_SEP_STRICT}\s*", re.IGNORECASE)
# Free-text clinical fields of a prescription/report, shown with the
# document's own details. Never invented: a document without them simply has
# no such item.
_COMPLAINT = re.compile(
    rf"\b(?:chief\s*complaints?|presenting\s*(?:complaints?|symptoms?)|complaints?|c\s*/\s*o)"
    rf"\s*{_SEP_STRICT}\s*"
    # A bare "symptoms" is an ordinary word in an instruction ("for allergy symptoms. If ...")
    # and so only counts as a label with a real colon — never with a period or a dash.
    r"|\bsymptoms?\s*:\s*",
    re.IGNORECASE,
)
_DIAGNOSIS = re.compile(
    rf"\b(?:provisional\s*diagnosis|final\s*diagnosis|diagnosis|dx|impression)\s*{_SEP_STRICT}\s*",
    re.IGNORECASE,
)
# "Name: Zainab" with no "Patient" in front of it. Never when the word before
# it says whose / what name it is ("Doctor Name:", "Clinic Name:", "Drug Name:").
_GENERIC_NAME = re.compile(rf"(?:^|(?<=\s))name\s*{_SEP}\s*", re.IGNORECASE)
_NAME_BELONGS_ELSEWHERE = re.compile(
    r"\b(?:doctor|dr|clinic|hospital|facility|lab|laboratory|drug|medicine|brand|generic|test)\s*$",
    re.IGNORECASE,
)


def _generic_name_match(line: str) -> re.Match[str] | None:
    for m in _GENERIC_NAME.finditer(line):
        if not _NAME_BELONGS_ELSEWHERE.search(line[: m.start()]):
            return m
    return None


# ── The advice / notes section ───────────────────────────────────────────
# The section starts at a HEADING — a notes word at the start of a line (after
# optional list-number / OCR junk symbols), or anywhere in a line when it is
# followed by an explicit colon. A notes word merely appearing inside a
# sentence ("...5 days as per instructions") is NOT a heading: that read
# used to open the section in the middle of a medicine line and sweep every
# following medicine into "advice".
_NOTES_WORDS = (
    r"(?:special\s+instructions?|doctor'?s\s+(?:advice|notes?)|"
    r"discharge\s+(?:advice|instructions?)|recommendations?|"
    r"advices?|instructions?|notes?|remarks?|comments?|interpretation)"
)
_NOTES_HEADING_AT_START = re.compile(
    rf"^(?:\d{{1,2}}[.)]\s*)?[\W_]*{_NOTES_WORDS}\b\s*[:\-–]?\s*", re.IGNORECASE
)
_NOTES_HEADING_WITH_COLON = re.compile(rf"\b{_NOTES_WORDS}\s*:\s*", re.IGNORECASE)
# A line that ENDS the section: another labelled field, or the page's
# footer/signature furniture. Advice is whatever sits under its heading up
# to the first of these — never the rest of the document.
_SECTION_LABELS = (
    r"(?:patient|name|age|sex|gender|address|ph|phone|tel|mobile|contact|email|doctor|"
    r"referred\s*by|consultant|specialization|speciality|specialisation|department|"
    r"report\s*date|prescription\s*date|date|collection\s*date|sample\s*date|"
    r"specimen\s*date|sample\s*collection\s*date|collected|received|diagnosis|dx|impression|"
    r"reference\s*range|lab\s*no|test\s*name|allerg(?:y|ies)|"
    r"(?:chief\s*|presenting\s*)?(?:complaints?|symptoms?)|c\s*/\s*o|investigations?|tests?|labs?|"
    r"bp|pulse|temp|temperature|weight|wt|height|signature|stamp|timings?|"
    r"clinic\s*hours|reg(?:istration)?|pmdc|mrn|cnic|"
    # the headings of an "other" document's own passages (see `_TOPIC_PATTERNS`)
    r"(?:clinical\s+|radiolog\w*\s+|examination\s+|operative\s+)?(?:findings?|observations?)|"
    r"conclusions?|procedures?|hospital\s+course|treatment\s+given|"
    r"condition\s+(?:at|on)\s+discharge)"
)
_SECTION_END_LABEL = re.compile(rf"^[\W_]*{_SECTION_LABELS}\.?\s*[:\-]", re.IGNORECASE)
_PAGE_FOOTER = re.compile(
    r"\btimings?\b|\bclinic\s+hours\b|\bclosed?\s+on\b|\bwww\.|https?://|@[\w-]+\.|"
    r"\bnot\s+valid\b|\bvalid\s+(?:for|until|upto)\b|\bmedico-?legal\b|\bpowered\s+by\b|"
    r"\bprinted\s+on\b|^[\W_]*(?:signature|stamp)\b|"
    # The sign-off after the advice list: not advice, and not part of it.
    r"\bfor\s+any\s+(?:queries|questions|query)\b|\bget\s+well\s+soon\b|\bthank\s*you\b|"
    r"\bcomputer[\s-]*generated\b|\bscan\s+for\s+online\b|\bdoes\s+not\s+require\b|"
    r"\bbest\s+wishes\b|\bwish\s+you\b",
    re.IGNORECASE,
)
_DOCTOR_TOKEN = re.compile(r"\bDr\.?\s+[A-Z][a-z]+")
_RX_MARKER = re.compile(r"^[\W_]*(?:rx|℞)\b", re.IGNORECASE)
_BULLET = re.compile(r"^[-•*~·»>–—]+\s*")
_SIGNATURE_BLOCK = re.compile(r"^(?:dr\.?\s|doctor\b)", re.IGNORECASE)
# At least one real word (2+ letters in ANY script — Unicode-aware, so Urdu
# notes are recognised too, not just English) — a line of stray OCR noise
# ("0 0 0 0 000", "a b c d e f", a row of repeated symbols) has none.
_REAL_WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def _strip_bullet(line: str) -> str:
    return _BULLET.sub("", line).strip()


def _looks_like_real_text(line: str) -> bool:
    """A crude, deliberately narrow OCR-garbage filter for free-text notes
    only — never applied to a labelled clinical field. It only ever drops a
    single line that has no real word in it at all; it cannot mistake a
    short-but-genuine remark for garbage as long as that remark contains one
    real word, and it never touches the lines around a rejected one."""
    return bool(_REAL_WORD.search(line))


def _age_only(raw: str) -> str:
    m = _AGE_DIGITS.search(raw)
    return m.group(0) if m else raw


def _first_match(line: str, *patterns: re.Pattern[str]) -> tuple[re.Match[str], int] | None:
    """The earliest-starting match among several label patterns on one
    line, so a line naming more than one field is read left to right."""
    best: tuple[re.Match[str], int] | None = None
    for pattern in patterns:
        m = pattern.search(line)
        if m and (best is None or m.start() < best[1]):
            best = (m, m.start())
    return best


# A value this much longer than the field could ever realistically BE is
# not a reading of that field at all — it is almost always the value
# swallowing a neighbouring label and ITS value too, because the label that
# should have stopped it (`_cut_at_next_label`) was itself too corrupted to
# recognise (observed: a "Patient ID :" label corrupted past recognition
# left "Male" absorbing everything up to the document's own patient ID —
# 27 characters, not even extreme-looking by itself, far too long for a
# field with only a handful of real possible values). Caps are deliberately
# per-field, not one global number: a gender or age genuinely never runs
# long, while an address legitimately can. Never invented or guessed at —
# an over-length value is dropped entirely (treated as "could not be
# confidently read"), exactly this module's existing philosophy for any
# other value it cannot trust, never truncated into a different, equally
# unverified value.
_MAX_VALUE_LEN: dict[str, int] = {
    "gender": 20,
    "age": 10,
    "patient_id": 30,
    "date_of_birth": 20,
    "document_title": 70,
    "phone": 25,
    "email": 60,
    "prescription_date": 20,
    "report_date": 20,
    "collection_date": 20,
    "received_date": 20,
    "qualification": 60,
    "specialization": 60,
    "complaint": 160,
    "diagnosis": 160,
}
_DEFAULT_MAX_VALUE_LEN = 100  # name/hospital/lab/doctor fields, address, anything not listed above


class _Collector:
    """Ordered, de-duplicated label -> value pairs (first occurrence wins —
    a repeated label later in the text, e.g. inside free-text notes, never
    overwrites the one already found).

    A value rejected by the length cap is recorded too, as `(key, "",
    True)`, rather than silently dropped — the caller (`_sections_out` in
    `service.py`) surfaces this as `SectionItem.unreadable` so the field
    shows as "attempted but not readable" instead of looking like it was
    never in the document at all. If a LATER line gives a clean value for
    the same key, it replaces the unreadable marker in place (the field's
    position in the list doesn't move) — a good read always wins over an
    earlier failed one."""

    def __init__(self) -> None:
        self.items: list[tuple[str, str, bool]] = []
        self._seen: set[str] = set()
        self._unreadable_index: dict[str, int] = {}

    def add(self, key: str, value: str) -> None:
        cleaned = _clean(value)
        if not cleaned or key in self._seen:
            return
        if len(cleaned) > _MAX_VALUE_LEN.get(key, _DEFAULT_MAX_VALUE_LEN):
            # implausibly long for this field — almost certainly a swallowed
            # boundary; never guessed or truncated, just flagged.
            if key not in self._unreadable_index:
                self._unreadable_index[key] = len(self.items)
                self.items.append((key, "", True))
            return
        if key in self._unreadable_index:
            self.items[self._unreadable_index.pop(key)] = (key, cleaned, False)
        else:
            self.items.append((key, cleaned, False))
        self._seen.add(key)

    def has(self, key: str) -> bool:
        return key in self._seen


def _extract_patient(lines: list[str]) -> list[tuple[str, str, bool]]:
    out = _Collector()
    for line in lines:
        if (
            (m := _PATIENT_NAME.search(line))
            or not out.has("patient_name")
            and (m := _PATIENT_LABEL.search(line))
            or not out.has("patient_name")
            and (m := _generic_name_match(line))
        ):
            out.add("patient_name", _cut_at_next_label(line, m.end()))
        if m := _AGE_GENDER.search(line):
            rest = _cut_at_next_label(line, m.end())
            if sep := _AGE_GENDER_VALUE_SEP.search(rest):
                out.add("age", _age_only(rest[: sep.start()]))
                out.add("gender", rest[sep.end() :])
            else:
                out.add("age", _age_only(rest))
        else:
            # Independent, not either/or: "Age: 7   Sex: F" names BOTH, and
            # reading only the first lost the gender.
            if m := _AGE.search(line):
                out.add("age", _age_only(_cut_at_next_label(line, m.end())))
            if m := _GENDER.search(line):
                out.add("gender", _cut_at_next_label(line, m.end()))
        if m := _PATIENT_ID.search(line):
            out.add("patient_id", _cut_at_next_label(line, m.end()))
        if m := _DOB.search(line):
            out.add("date_of_birth", _cut_at_next_label(line, m.end()))
    return out.items


def _patient_region_start(lines: list[str]) -> int | None:
    """The index of the first line naming the patient — everything before
    this is the facility's own letterhead; everything from here on is the
    patient's own block. Used so a labelled "Address:"/"Phone:" is credited
    to whichever one it actually sits in, instead of every contact label in
    the whole document being attributed to the facility regardless of where
    it appears (a document routinely has BOTH a facility address at the top
    and a patient's own address further down, under "Patient Name:")."""
    for i, line in enumerate(lines):
        if _PATIENT_NAME.search(line) or _PATIENT_LABEL.search(line) or _generic_name_match(line):
            return i
    return None


def _extract_contact(lines: list[str]) -> list[tuple[str, str, bool]]:
    """Phone / email / address — shared shape for the facility letterhead
    and (called again on a different slice of lines) the patient's own
    contact details; which one a given call's result is credited to depends
    entirely on which lines it is given, not anything in this function."""
    out = _Collector()
    for line in lines:
        if m := _PHONE.search(line):
            out.add("phone", _cut_at_next_label(line, m.end()))
        if m := _EMAIL_LABELLED.search(line):
            out.add("email", _cut_at_next_label(line, m.end()))
        elif not out.has("email") and (m := _EMAIL_BARE.search(line)):
            out.add("email", m.group(0))
        if m := _ADDRESS.search(line):
            out.add("address", _cut_at_next_label(line, m.end()))
    return out.items


def _extract_provider(lines: list[str]) -> list[tuple[str, str, bool]]:
    out = _Collector()
    for line in lines:
        if m := _REFERRED_BY.search(line):
            out.add("referred_by", _cut_at_next_label(line, m.end()))
            continue
        if m := _DOCTOR_LABELLED.search(line):
            out.add("doctor_name", _cut_at_next_label(line, m.end()))
        elif not out.has("doctor_name") and (m := _DOCTOR_SIGNATURE.search(line)):
            out.add("doctor_name", m.group(1))
        if m := _SPECIALIZATION_LABELLED.search(line):
            out.add("specialization", _cut_at_next_label(line, m.end()))
        if not out.has("qualification") and (m := _QUALIFICATION.search(line)):
            out.add("qualification", m.group(0))
            if not out.has("specialization") and (paren := _SPECIALIZATION_PAREN.search(line)):
                out.add("specialization", paren.group(1))
    return out.items


def _extract_report(lines: list[str], date_label: str) -> list[tuple[str, str, bool]]:
    out = _Collector()
    for line in lines:
        if (
            (m := _REPORT_DATE.search(line))
            or not out.has(date_label)
            and (m := _bare_date_match(line))
        ):
            out.add(date_label, _cut_at_next_label(line, m.end()))
        if m := _COLLECTION_DATE.search(line):
            out.add("collection_date", _cut_at_next_label(line, m.end()))
        if m := _RECEIVED_DATE.search(line):
            out.add("received_date", _cut_at_next_label(line, m.end()))
        if m := _COMPLAINT.search(line):
            out.add("complaint", _cut_at_next_label(line, m.end()))
        if m := _DIAGNOSIS.search(line):
            out.add("diagnosis", _cut_at_next_label(line, m.end()))
    # "Report Date" with its value in a column the reader reached much later
    if (
        date_label == "report_date"
        and not out.has(date_label)
        and (found := extractors.find_detached_report_date(lines))
    ):
        index, start, end = found
        out.add(date_label, lines[index][start:end])
    return out.items


# The document's own heading ("HbA1c & Glucose Panel", "Discharge Summary"). Only
# a short, label-free line near the top whose own words say what kind of document
# it is — never a facility name, a labelled field or a sentence.
_LAB_TITLE = re.compile(
    r"\b(?:panel|profile|culture|screen(?:ing)?|analysis|examination|studies|"
    r"complete\s+blood\s+count|cbc|hba1c|lipid|liver\s+function|renal\s+function|"
    r"kidney\s+function|thyroid|urine|stool|report)\b",
    re.IGNORECASE,
)
_OTHER_TITLE = re.compile(
    r"\b(?:discharge\s+summary|radiolog\w*|x-?\s?ray|ultra\s?sound|ultrasonograph\w*|"
    r"ct\s*scan|mri|ecg|echo(?:cardiogram)?|referral|medical\s+certificate|fitness|"
    r"sick\s+leave|consultation|clinical\s+(?:notes?|summary)|histopatholog\w*|biops\w*|"
    r"operative|progress\s+notes?|certificate|summary|letter|report)\b",
    re.IGNORECASE,
)
# A vaccination card's own heading. Never SHOWN as a title (a card has none); only used to
# tell that a letterhead's first line is the card's title and not a facility.
_VACCINE_TITLE = re.compile(
    r"\b(?:(?:vaccination|immuni[sz]ation|vaccine|epi)\s+(?:certificate|card|record|schedule|chart)|"
    r"certificate|record|card)\b",
    re.IGNORECASE,
)
_TITLE_SCAN_LINES = 12
_TITLE_MAX_LEN = 70


def _document_title(lines: list[str], doc_type: str) -> str | None:
    pattern = {DOC_TYPE_LAB: _LAB_TITLE, DOC_TYPE_VACCINE: _VACCINE_TITLE}.get(
        doc_type, _OTHER_TITLE
    )
    for line in lines[:_TITLE_SCAN_LINES]:
        candidate = _clean(line)
        letters = sum(ch.isalpha() for ch in candidate)
        if (
            len(candidate) < 6
            or len(candidate) > _TITLE_MAX_LEN
            or letters < 0.7 * len(candidate)
            or _ANY_LABEL_START.search(candidate)
            or _DOCTOR_TOKEN.search(candidate)
            or _STRONG_FACILITY.search(candidate)
            or not pattern.search(candidate)
        ):
            continue
        return candidate
    return None


# ── Allergies ────────────────────────────────────────────────────────────
# An allergy goes into the patient's safety record once confirmed, so this reader is
# strict where the others are forgiving: only a real LABEL opens it — "Allergies:",
# "Known drug allergy:", "Allergic to ..." — never the word "allergy" inside a
# sentence ("for allergy symptoms. if"), and a statement that there are NONE ("NKDA",
# "No known allergies", "Nil") produces nothing at all.
_ALLERGY_LABEL = re.compile(
    r"\b(?:known\s+|drug\s+|food\s+|medicine\s+)*allerg(?:y|ies)(?:\s+to)?\s*[:=]\s*"
    r"|\ballergic\s+to\s+(?=[A-Za-z])",
    re.IGNORECASE,
)
_NO_ALLERGIES = re.compile(
    r"^(?:nil|none|nothing|nkda|nka|nkfa|n\.?\s*/?\s*a\.?|not\s+known|negative|no\b.*|-+)$",
    re.IGNORECASE,
)
_ALLERGY_SEVERITY = re.compile(r"\b(mild|moderate|severe)\b", re.IGNORECASE)
_ALLERGY_TAIL_WORDS = re.compile(
    r"\s+(?:allerg(?:y|ic|ies)|intolerance|sensitivity)\s*$", re.IGNORECASE
)
_ALLERGY_ITEM_PAREN = re.compile(r"^(?P<sub>[^()]+?)\s*\((?P<rx>[^)]+)\)\s*$")
_ALLERGY_ITEM_DASH = re.compile(r"^(?P<sub>[^-–—:]+?)\s*[-–—:]\s*(?P<rx>.+)$")
_ALLERGY_SUBSTANCE_MAX = 60
_ALLERGY_REACTION_MAX = 80
_ALLERGY_ITEMS_MAX = 10


def _split_allergy_items(value: str) -> list[str]:
    # A comma inside brackets ("Penicillin (rash, hives)") belongs to that item.
    hidden = re.sub(r"\([^)]*\)", lambda m: m.group(0).replace(",", "\x00"), value)
    parts = re.split(r"\s*(?:,|;|&|\band\b|/)\s*", hidden, flags=re.IGNORECASE)
    return [p.replace("\x00", ",").strip() for p in parts if p.strip()]


def _allergy_from_item(item: str) -> dict[str, str] | None:
    reaction = ""
    if (m := _ALLERGY_ITEM_PAREN.match(item)) or (m := _ALLERGY_ITEM_DASH.match(item)):
        substance, reaction = m.group("sub"), m.group("rx")
    else:
        substance = item
    severity = ""
    if found := _ALLERGY_SEVERITY.search(reaction):
        severity = found.group(1).lower()
        reaction = _ALLERGY_SEVERITY.sub("", reaction)
    elif found := _ALLERGY_SEVERITY.match(substance):  # "Severe penicillin"
        severity = found.group(1).lower()
        substance = substance[found.end() :]
    substance = _ALLERGY_TAIL_WORDS.sub("", _clean(substance))
    reaction = _clean(reaction)
    # A real allergen is a short name, not a sentence, and has letters in it.
    if (
        not re.search(r"[A-Za-z]{2}", substance)
        or len(substance) > _ALLERGY_SUBSTANCE_MAX
        or len(substance.split()) > 5
        or _NO_ALLERGIES.match(substance)
    ):
        return None
    entry = {"allergy_substance": substance}
    if reaction and len(reaction) <= _ALLERGY_REACTION_MAX:
        entry["allergy_reaction"] = reaction
    if severity:
        entry["allergy_severity"] = severity
    return entry


def _extract_allergies(lines: list[str]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for line in lines:
        m = _ALLERGY_LABEL.search(line)
        if not m:
            continue
        value = _cut_at_next_label(line, m.end()).strip(" .")
        if not value or _NO_ALLERGIES.match(value):
            continue  # "NKDA", "No known allergies": nothing to record
        for item in _split_allergy_items(value):
            entry = _allergy_from_item(item)
            if entry is not None and entry not in found:
                found.append(entry)
            if len(found) >= _ALLERGY_ITEMS_MAX:
                return found
    return found


def _strip_notes_heading(text: str) -> str:
    """The text after a notes heading, with the heading's own decoration gone
    — including a compound heading like "Notes / Advice:"."""
    for _ in range(3):
        m = _NOTES_HEADING_AT_START.match(text.lstrip(" /&,:;-–"))
        if not m:
            break
        text = text.lstrip(" /&,:;-–")[m.end() :]
    return _strip_bullet(text.lstrip(" /&,:;-–"))


# Two-letter-or-shorter words that are real words; any other token that short
# at the EDGE of an advice line is OCR scrap ("'= |e symptoms persist or
# worsen, Oe" -> "symptoms persist or worsen,"). An ALL-CAPS token is kept
# (an acronym: "Go to ER").
_SHORT_REAL_WORDS = frozenset(
    {"a", "an", "to", "of", "in", "on", "it", "if", "or", "no", "as", "at", "be", "by", "do"}
    | {"go", "he", "is", "me", "my", "so", "up", "we", "i", "ok"}
)
_TOKEN_EDGE_PUNCT = "'\"|=.,;:!?"


def _is_junk_token(token: str) -> bool:
    if not any(ch.isalpha() for ch in token):
        return not any(ch.isdigit() for ch in token)  # a bare "¢" / "|" — but keep "2"
    core = token.strip(_TOKEN_EDGE_PUNCT)
    return len(core) <= 2 and core.lower() not in _SHORT_REAL_WORDS and not core.isupper()


def _trim_junk_edges(text: str) -> str:
    """`text` without scraps of OCR noise at its start or end. Edges only: a
    word in the middle of a line is never touched."""
    tokens = text.split()
    while tokens and _is_junk_token(tokens[0]):
        tokens.pop(0)
    while tokens and _is_junk_token(tokens[-1]):
        tokens.pop()
    return " ".join(tokens)


def _add_note(notes: list[str], text: str, *, bulleted: bool) -> None:
    """Append one advice line — tidied, and never as a fragment: a line that
    starts lowercase, is not its own bullet, and follows an unfinished line is
    that line's wrapped continuation ("...or worsen," / "please visit
    again.") and is joined to it instead of listed separately."""
    text = _trim_junk_edges(text)
    if sum(ch.isalpha() for ch in text) < 3:
        return
    if (
        notes
        and not bulleted
        and text[:1].islower()
        and not notes[-1].endswith((".", "!", "?", ":"))
    ):
        notes[-1] = f"{notes[-1]} {text}"
    else:
        notes.append(text)


def _looks_like_signature_line(line: str) -> bool:
    """A signature line even with OCR scraps in front of the name ("P. J Dr.
    Ayesha Malik", "0 Dr. Dr. Sara Ahmed"): short, with "Dr. <Name>" preceded
    only by tokens of two characters or fewer. A real sentence naming a
    doctor ("See Dr. Ahmed next week") has a real word in front and is not
    one."""
    if len(line) > 45:
        return False
    m = _DOCTOR_TOKEN.search(line)
    return bool(m) and all(len(token) <= 2 for token in line[: m.start()].split())


def _ends_notes(line: str) -> bool:
    """A line that cannot be part of the advice section: the signature block,
    another labelled field, page footer/closing line/furniture, or the Rx
    marker."""
    return bool(
        _SIGNATURE_BLOCK.match(line)
        or _looks_like_signature_line(line)
        or _QUALIFICATION.search(line)
        or _SECTION_END_LABEL.match(line)
        or _PAGE_FOOTER.search(line)
        or _RX_MARKER.match(line)
    )


def _extract_notes(lines: list[str], medicine_lines: frozenset[int] = frozenset()) -> list[str]:
    """The advice / notes section — ONLY advice.

    It starts at a heading (see `_NOTES_HEADING_AT_START`) and runs to the first
    line that cannot be advice: a medicine line (`medicine_lines`, from the
    extractor), another labelled field, a closing line ("Get well soon!"), the
    signature block or page footer. A prescription whose advice has no heading
    at all ("Drink plenty of fluids" under the medicine list) still gets it: the
    first advice-sounding line AFTER the last medicine opens an implicit
    section, closed by the same rules."""
    notes: list[str] = []
    started = False
    for index, line in enumerate(lines):
        if not started:
            heading = _NOTES_HEADING_AT_START.match(line) or _NOTES_HEADING_WITH_COLON.search(line)
            if heading and index not in medicine_lines:
                started = True
                first = _strip_notes_heading(line[heading.end() :])
                if first and _looks_like_real_text(first) and not _ends_notes(first):
                    _add_note(notes, first, bulleted=False)
            continue
        if index in medicine_lines or _ends_notes(line):
            break
        heading = _NOTES_HEADING_AT_START.match(line)
        stripped = _strip_notes_heading(line[heading.end() :]) if heading else _strip_bullet(line)
        if stripped and _looks_like_real_text(stripped):
            _add_note(notes, stripped, bulleted=bool(heading or _BULLET.match(line)))
    if started or not medicine_lines:
        return notes

    # No heading anywhere: look for advice written after the last medicine.
    last_medicine = max(medicine_lines)
    for line in lines[last_medicine + 1 :]:
        if _ends_notes(line):
            break
        stripped = _strip_bullet(line)
        if not _looks_like_real_text(stripped):
            continue
        if notes or extractors.looks_like_general_advice(stripped):
            # the first advice line opens it; the rest of the list follows
            _add_note(notes, stripped, bulleted=bool(_BULLET.match(line)))
    return notes


# ── The headed passages of an "other" medical document ───────────────────
# A discharge summary, a radiology report or a clinic letter is prose under headings
# ("Findings:", "Procedure:", "Hospital course:"). Such a heading, as the document printed it,
# opens a passage that runs to the next heading, labelled field, signature or page footer —
# never further than `_TOPIC_MAX_LINES` lines. Only the document's own words are shown under
# the document's own heading; nothing is summarised and nothing is guessed. (A heading that
# is advice — advice / instructions / recommendations — is the NOTES section, not a topic.)
_TOPIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        key,
        re.compile(
            rf"^(?:\d{{1,2}}[.)]\s*)?[\W_]*(?:{words})\s*(?::\s*|[-–]\s+|$)",
            re.IGNORECASE,
        ),
    )
    for key, words in (
        (
            "findings",
            r"(?:clinical\s+|radiolog\w*\s+|examination\s+|operative\s+)?(?:findings?|observations?)",
        ),
        ("conclusion", r"conclusions?"),
        ("procedure", r"procedures?(?:\s+(?:performed|done))?"),
        ("hospital_course", r"hospital\s+course|course\s+in\s+(?:the\s+)?hospital"),
        ("treatment_given", r"treatment\s+given|treatment\s+in\s+hospital"),
        ("discharge_condition", r"condition\s+(?:at|on)\s+discharge"),
    )
)
TOPIC_KEYS: tuple[str, ...] = tuple(key for key, _pattern in _TOPIC_PATTERNS)
_TOPIC_MAX_LINES = 12
_TOPIC_LINE_MAX = 300


def _topic_heading(line: str) -> tuple[str, str] | None:
    """`(topic key, the text after the heading on the same line)` if `line` opens a passage."""
    for key, pattern in _TOPIC_PATTERNS:
        if m := pattern.match(line):
            return key, line[m.end() :]
    return None


def _extract_topics(lines: list[str]) -> list[tuple[str, list[str]]]:
    topics: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    i = 0
    while i < len(lines):
        heading = _topic_heading(lines[i])
        i += 1
        if heading is None or heading[0] in seen:
            continue
        key, first = heading
        body: list[str] = []
        first = _strip_bullet(first)
        if first and _looks_like_real_text(first) and not _ends_notes(first):
            _add_note(body, first, bulleted=False)
        while i < len(lines) and len(body) < _TOPIC_MAX_LINES:
            line = lines[i]
            if _topic_heading(line) or _NOTES_HEADING_AT_START.match(line) or _ends_notes(line):
                break
            stripped = _strip_bullet(line)
            if _looks_like_real_text(stripped) and len(stripped) <= _TOPIC_LINE_MAX:
                _add_note(body, stripped, bulleted=bool(_BULLET.match(line)))
            i += 1
        if body:
            topics.append((key, body))
            seen.add(key)
    return topics


# ── The facility's own name ──────────────────────────────────────────────
# A letterhead rarely labels its own name, so it is found by what it IS: a
# line naming a hospital / clinic / centre / laboratory / pharmacy. "Strong"
# nouns settle it; "weak" ones (health, medical, care...) only ever
# complete a name that wrapped onto a second line.
_STRONG_FACILITY = re.compile(
    r"\b(?:hospital|clinics?|cent(?:re|er)s?|laborator(?:y|ies)|labs?|diagnostics?|"
    r"pharmacy|dispensary|institute|polyclinic)\b",
    re.IGNORECASE,
)
_WEAK_FACILITY = re.compile(
    r"\b(?:health(?:care)?|medical|medicare|care|welfare|trust|foundation|complex|surgical)\b",
    re.IGNORECASE,
)
_LETTERHEAD_LINES = 8
_FACILITY_NAME_MAX = 80


def _name_part(line: str) -> str:
    """The line up to its first labelled field ("Chughtai Lab   Report Date:
    28/07/2026" -> "Chughtai Lab")."""
    first = _first_match(
        line,
        _PATIENT_NAME,
        _PATIENT_LABEL,
        _AGE,
        _GENDER,
        _ADDRESS,
        _PHONE,
        _REPORT_DATE,
        _DATE_BARE,
    )
    return line if first is None else line[: first[1]]


# What can share one OCR line with the facility's name: its address or phone
# in the next column ("CITY HEALTH CLINIC Shop No. 12, Main Boulevard",
# "Sunrise Medical Centre —¢ +91 11 4567 8900"), a logo read as stray
# symbols or a letter. The name ends where the neighbouring column starts.
# ("House", "Floor", "St" are NOT cues: they occur inside real names.)
_NEIGHBOUR_COLUMN_WORD = re.compile(
    r"^(?:shop|plot|block|street|road|rd|sector|phase|avenue|lane|suite|flat|opposite|near|"
    r"ph|phone|tel|email|www)\.?$",
    re.IGNORECASE,
)
_SYMBOL_ONLY = re.compile(r"^[^\w&\-]+$")  # "&" and "-" belong inside names
_TRAILING_NAME_WORDS = frozenset({"of", "and", "the", "for"})


def _tidy_facility_text(raw: str) -> str:
    tokens = raw.split()
    # A logo or bullet read as leading stray symbols / a single lowercase letter.
    while len(tokens) > 1 and (
        _SYMBOL_ONLY.match(tokens[0]) or (len(tokens[0]) == 1 and tokens[0].islower())
    ):
        tokens.pop(0)
    # The neighbouring column starts at an address word, a token holding a
    # digit, or a stray symbol.
    for i in range(1, len(tokens)):
        token = tokens[i]
        if (
            any(ch.isdigit() for ch in token)
            or _NEIGHBOUR_COLUMN_WORD.match(token)
            or _SYMBOL_ONLY.match(token)
        ):
            tokens = tokens[:i]
            break
    # A capital letter glued onto an ALL-CAPS name is a logo glyph read as a
    # letter ("G CITY HEALTH CLINIC"). Never "A"/"I", which can be real words.
    if (
        len(tokens) > 2
        and len(tokens[0]) == 1
        and tokens[0].isupper()
        and tokens[0] not in "AI"
        and all(t.isupper() for t in tokens[1:] if any(ch.isalpha() for ch in t))
    ):
        tokens.pop(0)
    # Trailing scraps of noise after the name ("Medical Centre eae").
    while (
        len(tokens) > 2
        and tokens[-1].islower()
        and len(tokens[-1]) <= 3
        and tokens[-1] not in _TRAILING_NAME_WORDS
    ):
        tokens.pop()
    return " ".join(tokens)


def _facility_text(line: str) -> str:
    """The facility-name candidate in one OCR line, with the neighbouring
    column and logo noise cut away (see `_tidy_facility_text`)."""
    return _clean(_tidy_facility_text(_clean(_name_part(line))))


def _is_facility_candidate(line: str) -> bool:
    name = _facility_text(line)
    if not name or len(name) > _FACILITY_NAME_MAX:
        return False
    letters = sum(ch.isalpha() for ch in name)
    if letters < 3 or letters / len(name) < 0.6:
        return False  # a phone number, an id, a ruled line
    if _EMAIL_BARE.search(name) or re.search(r"www\.|https?://", name, re.IGNORECASE):
        return False
    if _NOTES_HEADING_AT_START.match(name) or _RX_MARKER.match(name):
        return False
    # The prescriber's own line, not the facility's.
    return bool(
        _STRONG_FACILITY.search(name)
        or not (_DOCTOR_SIGNATURE.search(name) or _QUALIFICATION.search(name))
    )


# A name that opens with a generic word is probably the second half of a
# logo that stacks its name over two lines ("Sunrise" / "Medical Centre").
_GENERIC_FIRST_WORD = re.compile(
    r"^(?:medical|health|healthcare|family|general|diagnostics?|specialist|surgical|community|"
    r"clinic|cent(?:re|er)|hospital|laborator(?:y|ies)|lab|pharmacy)$",
    re.IGNORECASE,
)
_NOT_A_NAME_WORD = frozenset(
    {"prescription", "patient", "health", "phone", "email", "address", "date", "report", "your"}
)


def _wrapped_name_start(top: list[str], position: int) -> str | None:
    """The first half of a name a logo stacked over two lines: the nearest
    letterhead line above `position` (looking back at most two lines, past
    scraps of noise) that is exactly ONE capitalised word."""
    for back in (position - 1, position - 2):
        if back < 0:
            return None
        text = _facility_text(top[back])
        if re.fullmatch(r"[A-Z][a-z]{3,}", text) and text.lower() not in _NOT_A_NAME_WORD:
            return text
        if len([w for w in text.split() if len(w) >= 3]) >= 2:
            return None  # a real line of its own: do not reach past it
    return None


def _facility_name(lines: list[str], patient_start: int | None) -> str | None:
    """The hospital / clinic / laboratory name from the letterhead (every line
    above the patient's own block, capped).

    The first line when it is a plain name (what this has always done); but a
    letterhead that opens with the doctor and puts the clinic second, wraps a
    long name over two lines, shares a line with its address or phone, or
    stacks a logo's name over two lines, is read correctly too instead of
    losing the name, keeping half of it, or keeping the address."""
    top = lines[
        : min(patient_start if patient_start is not None else len(lines), _LETTERHEAD_LINES)
    ]
    if not top:
        return None
    head = _facility_text(top[0]) if _is_facility_candidate(top[0]) else ""
    if head and _STRONG_FACILITY.search(head):
        return head
    for position, line in enumerate(top[1:], start=1):
        name = _facility_text(line)
        if _is_facility_candidate(line) and _STRONG_FACILITY.search(name):
            # "City Health" / "Medical Complex & Hospital": one name, wrapped.
            if position == 1 and head and _WEAK_FACILITY.search(head):
                return f"{head} {name}"
            words = name.split()
            if _GENERIC_FIRST_WORD.match(words[0]) and len(words) <= 3:
                lead = _wrapped_name_start(top, position)
                return f"{lead} {name}" if lead else name
            return name
    if not head:
        for line in top[1:]:
            if _is_facility_candidate(line) and _WEAK_FACILITY.search(_facility_text(line)):
                return _facility_text(line)
    return head or None


# ── Hospital or laboratory? ──────────────────────────────────────────────
# A laboratory report, and an "other" document, can name BOTH the hospital it was issued in
# and the laboratory that ran it ("Aga Khan University Hospital" / "Clinical Laboratory").
# A name is read as a laboratory by the words it uses; a hospital by its own.
_LAB_FACILITY = re.compile(
    r"\b(?:laborator(?:y|ies)|labs?|diagnostics?|patholog\w*|reference\s+lab\w*)\b", re.IGNORECASE
)
_HOSPITAL_FACILITY = re.compile(
    r"\b(?:hospital|clinics?|polyclinic|medical|health(?:care)?|medicare|dispensary|institute|"
    r"trust|welfare)\b",
    re.IGNORECASE,
)
# "Hospital: City Hospital", "Lab Name: Chughtai Lab", "Vaccination Centre: EPI Centre". "Lab No:"
# and "Clinic Hours:" are not names: after the label only an optional "name" may precede the colon.
_LABELLED_FACILITY = re.compile(
    r"\b(?:hospital|clinic|(?:medical\s+)?cent(?:re|er)|facility|institution|"
    r"laborator(?:y|ies)|lab|(?:vaccination|immuni[sz]ation)\s+cent(?:re|er)|issued\s+by)"
    r"\s*(?:name)?\s*[:=]\s*",
    re.IGNORECASE,
)


def _facility_key(name: str, doc_type: str) -> str:
    """Which facility field `name` is. A laboratory report whose only name is neither a
    laboratory's nor a hospital's is still a laboratory's letterhead (as it always was)."""
    if doc_type == DOC_TYPE_LAB:
        if _HOSPITAL_FACILITY.search(name) and not _LAB_FACILITY.search(name):
            return "hospital_name"
        return "lab_name"
    if doc_type == DOC_TYPE_OTHER and _LAB_FACILITY.search(name):
        return "lab_name"
    return "hospital_name"


def _looks_like_a_name(text: str) -> bool:
    """A short run of capitalised words ("Clinical Laboratory", "AGA KHAN LAB") — not a
    tagline ("Quality diagnostic services since"). Connectors may be lower case."""
    if ":" in text:
        return False  # "Sample Collection Centre: Gulshan" is a labelled field, not a name
    words = text.split()
    return len(words) <= 6 and all(
        w[:1].isupper() or w.casefold() in _TRAILING_NAME_WORDS or not w[:1].isalpha()
        for w in words
    )


def _plausible_facility(value: str) -> bool:
    letters = sum(ch.isalpha() for ch in value)
    return (
        3 <= letters
        and len(value) <= _FACILITY_NAME_MAX
        and not any(ch.isdigit() for ch in value)
        and not _EMAIL_BARE.search(value)
    )


def _facility_names(
    lines: list[str], patient_start: int | None, doc_type: str
) -> list[tuple[str, str]]:
    """The hospital / laboratory names of a document as `(key, value)`.

    A prescription and a vaccination card have one: the letterhead's (`hospital_name`). A
    laboratory report and an "other" document sort what they name into `hospital_name` and
    `lab_name`, find a second name of the other kind in the letterhead when there is one, and
    take a name the document LABELS ("Hospital: ...", "Lab Name: ...") over a guess."""
    found: dict[str, str] = {}
    name = _facility_name(lines, patient_start)
    if name and doc_type != DOC_TYPE_RX and _document_title([name], doc_type):
        # The letterhead's first line is the document's own title ("Medical Certificate",
        # "Complete Blood Count"), not a facility: with no facility named, none is shown.
        name = None
    if name:
        found[_facility_key(name, doc_type)] = name
    if doc_type == DOC_TYPE_RX:
        return list(found.items())  # a prescription: the letterhead's name, as it always was

    first = next(iter(found.values()), "").casefold()
    top = lines[
        : min(patient_start if patient_start is not None else len(lines), _LETTERHEAD_LINES)
    ]
    for line in top:
        candidate = _facility_text(line)
        if (
            _is_facility_candidate(line)
            and _STRONG_FACILITY.search(candidate)
            and _looks_like_a_name(candidate)
            and not (first and (candidate.casefold() in first or first in candidate.casefold()))
        ):
            found.setdefault(_facility_key(candidate, doc_type), candidate)
    for line in lines:
        if m := _LABELLED_FACILITY.search(line):
            if line[: m.start()].rstrip().casefold().endswith(("collection", "sample", "drop")):
                continue  # a collection point is not the facility
            value = _clean(_tidy_facility_text(_clean(_cut_at_next_label(line, m.end()))))
            if _plausible_facility(value):
                label = m.group(0).casefold()
                key = (
                    "lab_name"
                    if label.startswith("lab")
                    else "hospital_name"
                    if label.startswith(("hospital", "clinic", "medical", "cent", "facility"))
                    else _facility_key(value, doc_type)
                )
                found[key] = value
    # A laboratory report leads with the laboratory, everything else with the hospital.
    order = (
        ("lab_name", "hospital_name") if doc_type == DOC_TYPE_LAB else ("hospital_name", "lab_name")
    )
    return [(key, found[key]) for key in order if key in found]


def structure_document(text: str, doc_type: str) -> DocumentSections:
    """The OCR text of a document organised into named sections for display.

    Every document type gets the shared sections (facility, patient, doctor, the
    document's own dates and closing notes). On top of those: a prescription and
    an "other" medical document also get their vital signs; a laboratory report
    and an "other" document get their title; a vaccination record gets its
    vaccine doses. The clinical TABLES (medicines, lab results) are not here —
    they are the reviewable rows (`extractors.py`)."""
    if doc_type not in (DOC_TYPE_RX, DOC_TYPE_LAB, DOC_TYPE_VACCINE, DOC_TYPE_OTHER):
        return DocumentSections()

    lines = _lines(text)
    if not lines:
        return DocumentSections()

    # Where the facility's name is — see `_facility_name`. The patient's own
    # block (everything from the first line naming the patient) bounds the
    # letterhead above it.
    patient_start = _patient_region_start(lines)
    out = _Collector()
    for key, name in _facility_names(lines, patient_start, doc_type):
        out.add(key, name)
    facility: list[tuple[str, str, bool]] = out.items

    # A document's own contact details (phone/email/address) appear in TWO
    # different places depending on whose they are: the facility's own, in
    # its letterhead, ABOVE the patient's own name; the patient's own,
    # alongside their name/age/ID. Crediting every contact label in the
    # whole text to the facility — regardless of where it actually sits —
    # would misattribute the patient's own listed address to the clinic.
    facility.extend(_extract_contact(lines[:patient_start] if patient_start is not None else lines))

    patient = _extract_patient(lines)
    if patient_start is not None:
        patient.extend(_extract_contact(lines[patient_start:]))
    provider = _extract_provider(lines)
    date_label = "prescription_date" if doc_type == DOC_TYPE_RX else "report_date"
    report = _extract_report(lines, date_label)
    if doc_type in (DOC_TYPE_LAB, DOC_TYPE_OTHER) and (title := _document_title(lines, doc_type)):
        report.insert(0, ("document_title", title, False))
    medicine_lines = (
        frozenset(extractors.medicine_line_indexes(lines))
        if doc_type == DOC_TYPE_RX
        else frozenset()
    )
    notes = _extract_notes(lines, medicine_lines)

    return DocumentSections(
        facility=facility,
        patient=patient,
        provider=provider,
        report=report,
        vitals=vitals.extract_vitals(lines) if doc_type in (DOC_TYPE_RX, DOC_TYPE_OTHER) else [],
        entries=vaccines.extract_vaccine_entries(lines) if doc_type == DOC_TYPE_VACCINE else [],
        allergies=_extract_allergies(lines) if doc_type in (DOC_TYPE_RX, DOC_TYPE_OTHER) else [],
        notes=notes,
        topics=_extract_topics(lines) if doc_type == DOC_TYPE_OTHER else [],
    )


def value_limit(key: str) -> int:
    """The longest value the field `key` can plausibly have — the same caps the
    deterministic extraction applies (`_MAX_VALUE_LEN`), reused by the AI layer
    so both hold values to one standard."""
    return _MAX_VALUE_LEN.get(key, _DEFAULT_MAX_VALUE_LEN)


ALLERGY_KEYS = frozenset({"allergy_substance", "allergy_reaction", "allergy_severity"})


# The vocabulary an AI-read section may use — the same keys `structure_document`
# produces. Anything else in stored JSON is ignored, never rendered.
AI_SECTION_KEYS: dict[str, frozenset[str]] = {
    "facility": frozenset({"hospital_name", "lab_name", "phone", "email", "address"}),
    "patient": frozenset(
        {"patient_name", "patient_id", "age", "gender", "date_of_birth", "phone", "address"},
    ),
    "provider": frozenset({"doctor_name", "referred_by", "qualification", "specialization"}),
    "report": frozenset(
        {
            "prescription_date",
            "report_date",
            "collection_date",
            "received_date",
            "complaint",
            "diagnosis",
            "document_title",
        }
    ),
    "vitals": frozenset(vitals.VITAL_KEYS),
}
# Fields with a fixed SHAPE (an id, an age, a date, a phone number, an e-mail):
# when the deterministic reading found a value, it stands. The AI only fills one
# the extractor missed or could not read. For free text (names, addresses,
# complaint, diagnosis) the validated AI reading wins, since that is exactly
# where the OCR is noisiest and a model can tell a name from the noise around it.
_SHAPED_KEYS = frozenset(
    {
        "patient_id",
        "date_of_birth",
        "age",
        "gender",
        "phone",
        "email",
        "prescription_date",
        "report_date",
        "collection_date",
        "received_date",
        *vitals.VITAL_KEYS,
    }
)


def _merge_section(
    items: list[tuple[str, str, bool]], ai_values: dict[str, str]
) -> list[tuple[str, str, bool]]:
    merged = list(items)
    position = {key: index for index, (key, _value, _unreadable) in enumerate(merged)}
    for key, value in ai_values.items():
        if key not in position:
            merged.append((key, value, False))
            continue
        _key, current, unreadable = merged[position[key]]
        if key in _SHAPED_KEYS and current and not unreadable:
            continue
        merged[position[key]] = (key, value, False)
    return merged


def apply_ai_sections(parsed: DocumentSections, ai: object) -> DocumentSections:
    """`parsed` (the deterministic sections) with the stored AI reading
    (`ocr_job.ai_sections`, already validated against the OCR text when it was
    saved — see `ai_sections.py`) merged over it. A missing, malformed or
    unknown-version payload changes nothing, and every value is re-checked here
    for type, key and length, because stored JSON is data, not trusted code: the
    page can never render more than the deterministic path could have."""
    if not isinstance(ai, dict) or ai.get("version") != 1:
        return parsed

    def values(name: str) -> dict[str, str]:
        raw = ai.get(name)
        if not isinstance(raw, dict):
            return {}
        return {
            key: value.strip()
            for key, value in raw.items()
            if key in AI_SECTION_KEYS[name]
            and isinstance(value, str)
            and value.strip()
            and len(value) <= value_limit(key)
        }

    raw_entries = ai.get("entries")
    ai_entries: list[dict[str, str]] = []
    if isinstance(raw_entries, list):
        for raw in raw_entries[: vaccines.MAX_ENTRIES]:
            if not isinstance(raw, dict):
                continue
            entry = {
                key: value.strip()
                for key, value in raw.items()
                if key in vaccines.ENTRY_KEYS
                and isinstance(value, str)
                and value.strip()
                and len(value) <= 60
            }
            if entry.get("vaccine_name"):
                ai_entries.append(entry)

    raw_allergies = ai.get("allergies")
    ai_allergies: list[dict[str, str]] = []
    if isinstance(raw_allergies, list):
        for raw in raw_allergies[:_ALLERGY_ITEMS_MAX]:
            if not isinstance(raw, dict):
                continue
            entry = {
                key: value.strip()
                for key, value in raw.items()
                if key in ALLERGY_KEYS
                and isinstance(value, str)
                and value.strip()
                and len(value) <= _ALLERGY_REACTION_MAX
            }
            if entry.get("allergy_substance"):
                ai_allergies.append(entry)

    raw_notes = ai.get("notes")
    notes = (
        [n.strip() for n in raw_notes if isinstance(n, str) and n.strip()]
        if isinstance(raw_notes, list)
        else []
    )
    return DocumentSections(
        facility=_merge_section(parsed.facility, values("facility")),
        patient=_merge_section(parsed.patient, values("patient")),
        provider=_merge_section(parsed.provider, values("provider")),
        report=_merge_section(parsed.report, values("report")),
        vitals=_merge_section(parsed.vitals, values("vitals")),
        # A table is never mixed from two readings: the deterministic one, if it
        # found any doses, stands; the AI fills in only when it found none.
        entries=parsed.entries or ai_entries,
        # likewise a list, never mixed from two readings
        allergies=parsed.allergies or ai_allergies,
        notes=notes or parsed.notes,
        # headed passages are the document's own words under its own headings: read
        # deterministically only, never replaced by a model's paraphrase
        topics=parsed.topics,
    )
