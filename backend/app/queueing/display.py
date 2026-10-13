"""Pure display helpers for the queue (P6): no database."""

from __future__ import annotations


def mask_passport(value: str) -> str:
    """Hide every letter or digit but the last four: `CN-ZWSD-6GH8` becomes
    `••-••••-6GH8`. A value with four or fewer is hidden completely."""
    positions = [i for i, ch in enumerate(value) if ch.isalnum()]
    hidden = set(positions[:-4]) if len(positions) > 4 else set(positions)
    return "".join("•" if i in hidden else ch for i, ch in enumerate(value))
