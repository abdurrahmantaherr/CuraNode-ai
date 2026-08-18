"""Message catalogue for Urdu and English (FR28, NFR13).

Direction is derived from the locale here and nowhere else — no template or
component decides `dir` for itself (TDD 2.3 rule 2).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

Locale = Literal["en", "ur"]
SUPPORTED: tuple[Locale, ...] = ("en", "ur")
RTL_LOCALES: frozenset[str] = frozenset({"ur"})

_MESSAGES_DIR = Path(__file__).parent / "messages"


@lru_cache
def _catalogue(locale: str) -> dict[str, str]:
    path = _MESSAGES_DIR / f"{locale}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def normalise_locale(value: str | None) -> Locale:
    if value and value.lower() in SUPPORTED:
        return value.lower()  # type: ignore[return-value]
    return "en"


def translate(key: str, locale: str = "en", **params: Any) -> str:
    """Resolve a key, falling back to English then to the key itself.

    Returning the key rather than blank makes a missing translation obvious in
    the UI instead of silently rendering nothing.
    """
    locale = normalise_locale(locale)
    text = _catalogue(locale).get(key) or _catalogue("en").get(key) or key
    if params:
        for name, value in params.items():
            text = text.replace("{" + name + "}", str(value))
    return text


def direction(locale: str) -> Literal["rtl", "ltr"]:
    return "rtl" if normalise_locale(locale) in RTL_LOCALES else "ltr"


def missing_keys() -> dict[str, list[str]]:
    """Keys present in English but absent from another locale.

    Asserted by the test suite so an untranslated screen cannot ship (FR28).
    """
    base = set(_catalogue("en"))
    return {
        loc: sorted(base - set(_catalogue(loc))) for loc in SUPPORTED if loc != "en"
    }
