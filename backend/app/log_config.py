"""Structured logging with PII redaction (TDD 7.8, SPEC BL-14).

The redaction processor is the whole point of this module: passwords, tokens,
emails, phone numbers, and names must never reach a log at ANY level, including
DEBUG. Redaction is applied to nested structures too, because a dict logged as
event context is exactly how a secret escapes.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

# SPEC BL-14. `otp` is retained defensively: the field no longer exists, but a
# future reintroduction must not silently start logging it.
REDACTED_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "password_confirm",
        "otp",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "set-cookie",
        "email",
        "phone_e164",
        "phone",
        "full_name",
        "password_hash",
        # OAuth (PKCE): a leaked code/verifier is a replayable credential, and
        # state is the CSRF-binding secret for the flow.
        "code",
        "state",
        "code_verifier",
    }
)

PLACEHOLDER = "[redacted]"


def _redact(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return value
    if isinstance(value, dict):
        return {
            k: (PLACEHOLDER if k.lower() in REDACTED_KEYS else _redact(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_redact(v, depth + 1) for v in value)
    return value


def redaction_processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    return _redact(event_dict)  # type: ignore[return-value]


def configure_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redaction_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "curanode") -> Any:
    return structlog.get_logger(name)
