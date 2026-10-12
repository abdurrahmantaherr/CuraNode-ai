"""Request-shape constants for the verification module."""

from __future__ import annotations

REASONS: tuple[str, ...] = (
    "number_not_found",
    "name_mismatch",
    "licence_invalid",
    "not_at_clinic",
    "other",
)
