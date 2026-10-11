"""The provider chain behind `ai_understanding`: Gemini first, then Groq, then
OpenRouter (order is `AI_PROVIDER_ORDER`) — so one provider's quota running
out, an outage, or a bad response no longer means the AI layer is simply off.

What this module is, deliberately and only:

* A cooldown ledger. A provider that just failed with a rate limit / quota
  error is skipped for `AI_PROVIDER_COOLDOWN_S`; one that failed on its key or
  model for longer; a timeout or 5xx briefly. Without this an exhausted
  free tier would cost a full timeout on EVERY upload.
* One OpenAI-compatible JSON call, shared by Groq and OpenRouter.
* `run_chain`, which walks the providers in order under one overall time
  ceiling and returns the first usable answer.

What it is not: it never decides whether an answer is TRUSTED. Whatever a
provider returns goes through the same deterministic `_reconcile` checks in
`ai_understanding` as before, so a fallback model is held to exactly the
standard Gemini is — and when every provider fails, the caller falls back to
the deterministic extraction it already has. "Never fails" here means "always
degrades to a working result", not "every provider is always up".

Privacy: only OCR TEXT (the same text Gemini already receives) is ever sent —
never an image, never a patient identifier from outside the document text.
Keys, prompts and responses are never logged; a failure logs the provider
name, an error class/status, nothing else.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import httpx
from pydantic import BaseModel

from ..log_config import get_logger
from ..settings import settings

log = get_logger()

ALL_PROVIDERS = ("gemini", "groq", "openrouter")

_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Cooldown by failure kind (seconds), other than a rate limit, which uses
# `settings.ai_provider_cooldown_s` (or the provider's own Retry-After).
_COOLDOWN_AUTH_OR_MODEL_S = 300  # a wrong key / retired model will not fix itself in a minute
_COOLDOWN_TRANSIENT_S = 20  # timeout, 5xx, malformed answer
_MAX_RETRY_AFTER_S = 600

_cooldown_until: dict[str, float] = {}


def reset_state() -> None:
    """Forget every cooldown (tests; a deploy that rotated a key)."""
    _cooldown_until.clear()


def in_cooldown(name: str) -> bool:
    return _cooldown_until.get(name, 0.0) > time.monotonic()


def start_cooldown(name: str, seconds: float) -> None:
    _cooldown_until[name] = max(_cooldown_until.get(name, 0.0), time.monotonic() + seconds)


def _status_of(exc: BaseException) -> int | None:
    """An HTTP-ish status from whichever client raised: httpx
    (`response.status_code`) or google-genai (`.code`)."""
    response = getattr(exc, "response", None)
    for candidate in (getattr(response, "status_code", None), getattr(exc, "code", None)):
        if isinstance(candidate, int):
            return candidate
    return None


def _retry_after(exc: BaseException) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    try:
        value = float(headers.get("retry-after")) if headers else None
    except (TypeError, ValueError):
        return None
    return min(value, _MAX_RETRY_AFTER_S) if value and value > 0 else None


def note_failure(name: str, exc: BaseException | None = None, *, reason: str = "") -> str:
    """Record that `name` just failed and start the right cooldown for HOW it
    failed. Returns the failure kind (also what gets logged). Never logs the
    exception's message — only its class and status."""
    status = _status_of(exc) if exc is not None else None
    text = str(exc) if exc is not None else ""
    if status == 429 or "RESOURCE_EXHAUSTED" in text or "rate limit" in text.lower():
        kind = "rate_limited"
        seconds = (
            _retry_after(exc) if exc is not None else None
        ) or settings.ai_provider_cooldown_s
    elif status in (401, 403, 404) and exc is not None:
        kind = "auth_or_model"
        seconds = _COOLDOWN_AUTH_OR_MODEL_S
    elif isinstance(exc, TimeoutError | asyncio.TimeoutError | httpx.TimeoutException):
        kind = "timeout"
        seconds = _COOLDOWN_TRANSIENT_S
    else:
        kind = reason or "error"
        seconds = _COOLDOWN_TRANSIENT_S
    start_cooldown(name, seconds)
    log.warning(
        "ai_provider_failed",
        provider=name,
        kind=kind,
        status=status,
        error=type(exc).__name__ if exc is not None else None,
    )
    return kind


def provider_order() -> list[str]:
    """`AI_PROVIDER_ORDER`, de-duplicated, unknown names dropped. A provider
    left out of the list is never used, even when its key is set."""
    seen: list[str] = []
    for raw in settings.ai_provider_order.split(","):
        name = raw.strip().lower()
        if name in ALL_PROVIDERS and name not in seen:
            seen.append(name)
    return seen


def has_key(name: str) -> bool:
    return bool(getattr(settings, f"{name}_api_key", ""))


def configured_providers() -> list[str]:
    return [name for name in provider_order() if has_key(name)]


async def run_chain[T](
    attempts: Sequence[tuple[str, Callable[[], Awaitable[T | None]]]],
) -> T | None:
    """The first non-`None` answer from `attempts` (already in order and
    already limited to configured providers), skipping any in cooldown and
    stopping once `AI_TOTAL_TIMEOUT_S` has been spent. Each attempt must
    itself never raise (it reports failure by returning `None` after calling
    `note_failure`); anything that does raise is treated as a failure too."""
    deadline = time.monotonic() + settings.ai_total_timeout_s
    for name, attempt in attempts:
        if in_cooldown(name):
            log.info("ai_provider_skipped", provider=name, why="cooldown")
            continue
        if time.monotonic() >= deadline:
            log.warning("ai_chain_deadline_reached")
            break
        try:
            result = await attempt()
        except Exception as exc:  # noqa: BLE001 - a provider bug must never break the pipeline
            note_failure(name, exc)
            continue
        if result is not None:
            log.info("ai_provider_used", provider=name)
            return result
    return None


# ── OpenAI-compatible JSON call (Groq, OpenRouter) ───────────────────────
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


def _http_client(timeout_s: float) -> httpx.AsyncClient:
    """Indirection so tests can swap in an `httpx.MockTransport`."""
    return httpx.AsyncClient(timeout=timeout_s)


def schema_instruction(model: type[BaseModel]) -> str:
    """The shape the model must answer in. Gemini gets its schema through its
    native structured-output feature; the OpenAI-compatible providers only
    reliably offer "some JSON object", so the exact schema is spelled out in
    the prompt and the answer is validated against the same Pydantic model
    afterwards either way."""
    schema = json.dumps(model.model_json_schema(), separators=(",", ":"))
    return (
        "\n\nOUTPUT FORMAT: reply with ONE JSON object and nothing else — no prose, no "
        'markdown fences. It must validate against this JSON Schema exactly (use "" for '
        f"unknown strings, never omit a key): {schema}"
    )


def parse_json_answer(content: str, model: type[BaseModel]) -> BaseModel:
    """Validate a provider's text answer against `model`, tolerating the one
    habit models have even in JSON mode: wrapping the object in a code fence."""
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    return model.model_validate_json(text)


async def openai_compatible_json(
    *,
    name: str,
    url: str,
    api_key: str,
    model_id: str,
    system: str,
    contents: str,
    response_model: type[BaseModel],
    extra_headers: dict[str, str] | None = None,
) -> Any | None:
    """One chat-completions call asking for a JSON object, validated against
    `response_model`. Returns the validated model or `None` — never raises."""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    headers.update(extra_headers or {})
    body = {
        "model": model_id,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system + schema_instruction(response_model)},
            {"role": "user", "content": contents},
        ],
    }
    try:
        async with _http_client(settings.ai_provider_timeout_s) as client:
            response = await asyncio.wait_for(
                client.post(url, headers=headers, json=body),
                timeout=settings.ai_provider_timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        return parse_json_answer(content, response_model)
    except Exception as exc:  # noqa: BLE001 - any failure means "next provider", never a crash
        note_failure(name, exc, reason="invalid_response")
        return None


async def call_groq(contents: str, system: str, response_model: type[BaseModel]) -> Any | None:
    return await openai_compatible_json(
        name="groq",
        url=_GROQ_URL,
        api_key=settings.groq_api_key,
        model_id=settings.groq_model,
        system=system,
        contents=contents,
        response_model=response_model,
    )


async def call_openrouter(
    contents: str, system: str, response_model: type[BaseModel]
) -> Any | None:
    return await openai_compatible_json(
        name="openrouter",
        url=_OPENROUTER_URL,
        api_key=settings.openrouter_api_key,
        model_id=settings.openrouter_model,
        system=system,
        contents=contents,
        response_model=response_model,
        extra_headers={"X-Title": "CuraNode-AI"},
    )
