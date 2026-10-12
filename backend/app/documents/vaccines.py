"""Vaccination records read from a document's OCR text — display only.

A vaccination card or certificate lists doses: which vaccine, which dose, when it
was given, the batch, the site, and when the next one is due. There is no table
for these in the shared database (the field names `ocr_extraction` may hold are
fixed), so — by design — they are NOT reviewable rows: they are computed from the
OCR text each time a page is shown, exactly like the facility / patient / doctor
sections, and shown read-only.

Deterministic, like every extractor here: a vaccine is recognised by NAME (a list
of the vaccines that appear on Pakistani EPI cards and travel/adult certificates,
matched as written), and the other columns by SHAPE (a date, a batch label, a site
word) or by the table's own header row. A line is only an entry if it names a
vaccine; nothing is ever inferred about a vaccine the document does not name, and a
column the document does not show is simply left blank — never filled in.
"""

from __future__ import annotations

import re

# The columns shown, in order. Keys are i18n keys (`documents.field.<key>`).
ENTRY_KEYS: tuple[str, ...] = ("vaccine_name", "dose", "date_given", "batch_no", "site", "next_due")
MAX_ENTRIES = 40
_MAX_CELL_LEN = 60

_NAMES = (
    r"bcg|opv|ipv|(?:oral\s+)?polio(?:\s*\(?(?:opv|ipv)\)?)?|"
    r"hep(?:atitis)?[\s-]*[ab]|hbv|"
    r"penta(?:valent)?|hexa(?:valent)?|dpt|dtp|dtap|tdap|td|tt|tetanus(?:\s+toxoid)?|diphtheria|pertussis|"
    r"hib|pcv(?:\s*-?\s*1[03])?|pneumococcal(?:\s+conjugate)?|rota(?:virus)?|"
    r"measles(?:[\s-]*rubella)?|mmr|mr|rubella|"
    r"typhoid(?:\s+conjugate)?|tcv|varicella|chicken\s*pox|hpv|"
    r"influenza|flu|rabies|meningococcal|men(?:acwy|b)|japanese\s+encephalitis|yellow\s+fever|cholera|"
    r"covid(?:[\s-]*19)?|corona(?:virus)?|pfizer|moderna|sinopharm|sinovac|astra\s*zeneca|cansino"
)
_VACCINE = re.compile(rf"(?<![A-Za-z])(?:{_NAMES})(?![A-Za-z])", re.IGNORECASE)

_DATE = (
    r"\d{1,2}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{2,4}|"
    r"\d{4}\s*[-/.]\s*\d{1,2}\s*[-/.]\s*\d{1,2}|"
    r"\d{1,2}(?:st|nd|rd|th)?[\s-]+[A-Za-z]{3,9}\.?,?[\s-]+\d{2,4}"
)
_DATE_RE = re.compile(rf"(?<!\d)(?:{_DATE})(?!\d)")
_NEXT_DUE = re.compile(
    rf"\b(?:next(?:\s*dose)?(?:\s*due)?|due(?:\s*(?:on|date))?|repeat|booster\s*due)\s*[:=\-]?\s*"
    rf"({_DATE})",
    re.IGNORECASE,
)
_GIVEN = re.compile(
    rf"\b(?:given|administered|date(?:\s*given)?|vaccinated(?:\s*on)?)\s*[:=\-]?\s*({_DATE})",
    re.IGNORECASE,
)
_BATCH = re.compile(
    r"\b(?:batch|lot)\s*(?:no\.?|number|#)?\s*[:=\-]?\s*([A-Za-z0-9][A-Za-z0-9\-/]{2,19})",
    re.IGNORECASE,
)
_SITE = re.compile(
    r"\b((?:left|right|l\.?|r\.?)\s*(?:upper\s*)?(?:arm|thigh|deltoid|buttock|gluteal|shoulder))\b|"
    r"\bsite\s*[:=\-]\s*([A-Za-z][A-Za-z ]{2,24})",
    re.IGNORECASE,
)
_DOSE = re.compile(
    r"\b(booster|birth\s*dose|final\s*dose)\b|"
    r"\b((?:1st|2nd|3rd|[4-9]th|first|second|third|fourth)\s*dose)\b|"
    r"\bdose\s*(?:no\.?|number|#)?\s*[:=\-]?\s*([1-9])\b|"
    r"\bd\s?([1-9])\b",
    re.IGNORECASE,
)
# "OPV 2", "Penta-3", "PCV-10 / 2": a small number glued to the name is its dose.
_NAME_DOSE = re.compile(r"[\s-]+([1-5])(?![\d/.\-])")

# A header row names several columns and no vaccine ("Vaccine | Dose | Date | Batch").
_HEADER_WORDS = {
    "vaccine": "vaccine_name",
    "vaccination": "vaccine_name",
    "name": "vaccine_name",
    "dose": "dose",
    "date": "date_given",
    "given": "date_given",
    "date given": "date_given",
    "administered": "date_given",
    "batch": "batch_no",
    "batch no": "batch_no",
    "lot": "batch_no",
    "site": "site",
    "next due": "next_due",
    "next dose": "next_due",
    "due": "next_due",
    "due date": "next_due",
}
_CELL_SPLIT = re.compile(r"\s*\|\s*|\t|\s{3,}")


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" \t.,;:|-")[:_MAX_CELL_LEN]


def _cells(line: str) -> list[str]:
    return [c for c in (_clean(part) for part in _CELL_SPLIT.split(line)) if c]


def _header_map(line: str) -> list[str | None] | None:
    """Column keys for a table header line, or `None` if `line` is not one."""
    if _VACCINE.search(line):
        return None
    cells = _cells(line)
    mapped = [_HEADER_WORDS.get(re.sub(r"[^a-z ]", "", c.lower()).strip()) for c in cells]
    return mapped if len(cells) >= 3 and sum(k is not None for k in mapped) >= 3 else None


def _dose(text: str, name_end: int) -> str:
    if m := _DOSE.search(text):
        if m.group(1):
            return _clean(m.group(1)).capitalize()
        if m.group(2):
            return _clean(m.group(2))
        return f"Dose {m.group(3) or m.group(4)}"
    if m := _NAME_DOSE.match(text, name_end):
        return f"Dose {m.group(1)}"
    return ""


def _from_labels(line: str, name: re.Match[str]) -> dict[str, str]:
    entry: dict[str, str] = {"vaccine_name": _clean(name.group(0))}
    next_due = _NEXT_DUE.search(line)
    given = _GIVEN.search(line)
    if next_due:
        entry["next_due"] = _clean(next_due.group(1))
    if given:
        entry["date_given"] = _clean(given.group(1))
    elif dates := [
        m for m in _DATE_RE.finditer(line) if not (next_due and m.start() >= next_due.start())
    ]:
        entry["date_given"] = _clean(dates[0].group(0))
    if dose := _dose(line, name.end()):
        entry["dose"] = dose
    if batch := _BATCH.search(line):
        entry["batch_no"] = _clean(batch.group(1))
    if site := _SITE.search(line):
        entry["site"] = _clean(site.group(1) or site.group(2))
    return entry


def _from_columns(cells: list[str], columns: list[str | None]) -> dict[str, str]:
    entry: dict[str, str] = {}
    for cell, key in zip(cells, columns, strict=False):
        if key is None or key in entry:
            continue
        if key in ("date_given", "next_due"):
            if m := _DATE_RE.search(cell):
                entry[key] = _clean(m.group(0))
        elif key == "dose":
            entry[key] = f"Dose {cell}" if re.fullmatch(r"[1-9]", cell) else _clean(cell)
        else:
            entry[key] = _clean(cell)
    return entry


# ── A certificate written as "Label: value" lines ────────────────────────
# "Vaccine: Typhoid Conjugate (TCV) / Dose: 1 / Date of vaccination: 12-03-2026 / Batch No: T4471
# / Next dose due: 12-03-2027" is one dose, spread over several lines, and only the first names
# the vaccine — so a reader that needs the vaccine's name on the same line as its date finds
# nothing. Here a "Vaccine:" label opens a dose, and the labelled lines under it fill it in.
_KV_KINDS = (
    ("vaccine_name", r"vaccine(?:\s*(?:name|type|administered))?|name\s+of\s+(?:the\s+)?vaccine"),
    (
        "date_given",
        (
            r"date\s*of\s*(?:vaccination|administration|dose|immuni[sz]ation)|vaccination\s*date|"
            r"immuni[sz]ation\s*date|date\s*(?:given|administered|vaccinated)|"
            r"administered\s*(?:on|date)"
        ),
    ),
    ("next_due", r"next\s*(?:dose\s*)?(?:due\s*)?(?:date|on)?|due\s*date|booster\s*due"),
    ("dose", r"dose(?:\s*(?:no\.?|number|#))?"),
    ("batch_no", r"(?:batch|lot)\s*(?:no\.?|number|#)?"),
    ("site", r"(?:injection\s*)?site"),
    ("date_bare", r"date"),
)
_KV_LABEL = re.compile(
    r"(?<![A-Za-z])(?:"
    + "|".join(f"(?P<{key}>{words})" for key, words in _KV_KINDS)
    + r")\s*[:=]\s*",
    re.IGNORECASE,
)
_BATCH_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-/]{2,19}")


def _kv_value(kind: str, raw: str) -> str:
    value = _clean(raw)
    if kind in ("date_given", "next_due", "date_bare"):
        m = _DATE_RE.search(value)
        return _clean(m.group(0)) if m else ""
    if kind == "dose":
        return f"Dose {value}" if re.fullmatch(r"[1-9]", value) else value
    if kind == "batch_no":
        m = _BATCH_VALUE.match(value)
        return _clean(m.group(0)) if m else ""
    if kind == "vaccine_name" and sum(ch.isalpha() for ch in value) < 2:
        return ""
    return value


def _from_key_values(lines: list[str]) -> list[dict[str, str]]:
    """Doses written as labelled lines (see above). A dose is only ever filled from lines
    UNDER its own "Vaccine:" label; labelled facts printed before any vaccine are given to the
    dose only when the document has exactly one."""
    entries: list[dict[str, str]] = []
    before: dict[str, str] = {}  # facts printed ahead of the first "Vaccine:" line
    for line in lines:
        labels = list(_KV_LABEL.finditer(line))
        for position, m in enumerate(labels):
            end = labels[position + 1].start() if position + 1 < len(labels) else len(line)
            kind = next(k for k, _ in _KV_KINDS if m.group(k) is not None)
            value = _kv_value(kind, line[m.end() : end])
            if not value:
                continue
            if kind == "vaccine_name":
                entries.append({"vaccine_name": value})
                continue
            key = "date_given" if kind == "date_bare" else kind
            target = entries[-1] if entries else before
            if kind == "date_bare" and not entries:
                continue  # a bare "Date:" ahead of any vaccine is the document's date, not a dose's
            target.setdefault(key, value)
    if len(entries) == 1:
        for key, value in before.items():
            entries[0].setdefault(key, value)
    return entries[:MAX_ENTRIES]


def extract_vaccine_entries(lines: list[str]) -> list[dict[str, str]]:
    """The vaccine doses named in `lines`, one dict per dose (keys from
    `ENTRY_KEYS`; a column the document does not show is simply absent)."""
    # A certificate of labelled lines gives every dose its own details; use it when it actually
    # found some (a lone "Vaccine: BCG" line says no more than the line reader would).
    labelled = _from_key_values(lines)
    if any(len(entry) > 1 for entry in labelled):
        return labelled
    columns: list[str | None] | None = None
    entries: list[dict[str, str]] = []
    for line in lines:
        if header := _header_map(line):
            columns = header
            continue
        name = _VACCINE.search(line)
        if name is None or len(line) > 160:
            continue
        cells = _cells(line)
        if columns and len(cells) >= 2:
            entry = _from_columns(cells, columns)
            if not entry.get("vaccine_name"):
                entry["vaccine_name"] = _clean(name.group(0))
        else:
            entry = _from_labels(line, name)
        # A line that merely MENTIONS a vaccine inside a sentence ("Polio vaccine
        # is important for children") carries nothing else; a real entry has a
        # dose, date, batch or site — or is a short name-only list item.
        facts = [k for k in entry if k != "vaccine_name"]
        if not facts and len(line) > 40:
            continue
        if entry not in entries:
            entries.append(entry)
        if len(entries) >= MAX_ENTRIES:
            break
    return entries
