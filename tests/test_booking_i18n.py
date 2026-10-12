"""P5: every string the booking pages show exists in English and Urdu (FR28)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from app.i18n.catalogue import missing_keys

ROOT = Path(__file__).resolve().parents[1]
MESSAGES = ROOT / "backend" / "app" / "i18n" / "messages"
TEMPLATE_DIRS = [ROOT / "frontend" / "templates" / "booking"]
TEMPLATE_FILES = [ROOT / "frontend" / "templates" / "doctor" / "_appointments_today.html"]
PYTHON_FILES = [
    ROOT / "backend" / "app" / "web" / "booking.py",
    ROOT / "backend" / "app" / "booking" / "display.py",
]

IN_TEMPLATE = re.compile(r"""\bt\(\s*'([a-z0-9_.]+)'""")
IN_PYTHON = re.compile(r'"((?:booking|nav|errors)\.[a-z0-9_.]+)"')
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


def catalogue(locale: str) -> dict[str, str]:
    return json.loads((MESSAGES / f"{locale}.json").read_text(encoding="utf8"))


def keys_used() -> set[str]:
    used: set[str] = set()
    templates = [p for d in TEMPLATE_DIRS for p in d.glob("*.html")] + TEMPLATE_FILES
    for path in templates:
        used |= set(IN_TEMPLATE.findall(path.read_text(encoding="utf8")))
    for path in PYTHON_FILES:
        used |= set(IN_PYTHON.findall(path.read_text(encoding="utf8")))
    # Looked up by building the key: `booking.status.<status>` and `weekday.<n>`.
    used |= {f"booking.status.{s}" for s in ("booked", "checked_in", "done", "cancelled")}
    used |= {f"weekday.{n}" for n in range(7)}
    # Used by the nav builders in web/router.py, which this scan does not read.
    used |= {"nav.find_doctor", "nav.appointments"}
    return used


def test_the_scan_finds_the_booking_keys():
    used = keys_used()
    assert len(used) > 50
    assert {"booking.find.title", "booking.list.title", "booking.doctor_day.title"} <= used


def test_every_key_the_booking_pages_use_exists_in_both_languages():
    en, ur = catalogue("en"), catalogue("ur")
    missing = sorted(k for k in keys_used() if k not in en or k not in ur)
    assert not missing, missing


def test_no_key_is_missing_from_urdu_anywhere():
    assert missing_keys()["ur"] == []


@pytest.mark.parametrize(
    "prefix", ["booking.", "errors.booking_", "nav.find_doctor", "nav.appointments"]
)
def test_placeholders_match_between_the_languages(prefix):
    en, ur = catalogue("en"), catalogue("ur")
    for key in (k for k in en if k.startswith(prefix)):
        assert set(PLACEHOLDER.findall(en[key])) == set(PLACEHOLDER.findall(ur[key])), key


def test_the_urdu_text_is_not_a_copy_of_the_english():
    en, ur = catalogue("en"), catalogue("ur")
    same = sorted(k for k in en if k.startswith(("booking.", "errors.booking_")) and en[k] == ur[k])
    assert not same, same
