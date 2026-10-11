"""The AI provider chain (`documents/ai_providers.py` + `ai_understanding`):
Gemini -> Groq -> OpenRouter, with cooldowns. No test touches the network —
Gemini is a fake client, Groq/OpenRouter are an `httpx.MockTransport`.
"""

from __future__ import annotations

import json

import httpx
import pytest
from app.documents import ai_providers
from app.documents import ai_understanding as ai
from app.settings import settings

RAW_TEXT = "Tab. Metformin 500mg twice daily for 30 days"
EXISTING = [
    {"medicine": "Metformin", "dosage": "500 mg", "frequency": "twice daily", "duration": "30 days"}
]


def _answer(medicine="Metformin") -> dict:
    return {
        "medicines": [
            {
                "medicine": medicine,
                "dosage": "500 mg",
                "frequency": "twice daily",
                "duration": "30 days",
                "medicine_certainty": "high",
                "dosage_certainty": "high",
                "frequency_certainty": "high",
                "duration_certainty": "high",
            }
        ]
    }


# ── Gemini stand-in (same shape the real google-genai call has) ──────────
class _GeminiError(Exception):
    def __init__(self, code: int, text: str = "") -> None:
        super().__init__(text)
        self.code = code


class _FakeGemini:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.calls = 0
        self.aio = type("Aio", (), {"models": self})()

    async def generate_content(self, *, model, contents, config):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return type("R", (), {"parsed": self.outcome})()


def _gemini(monkeypatch, outcome) -> _FakeGemini:
    monkeypatch.setattr(settings, "gemini_api_key", "g-key")
    fake = _FakeGemini(outcome)
    monkeypatch.setattr(ai, "_get_client", lambda: fake)
    return fake


# ── Groq / OpenRouter stand-in ───────────────────────────────────────────
class _Http:
    """Records requests and replies per host via `httpx.MockTransport`."""

    def __init__(self, monkeypatch, handlers: dict[str, object]) -> None:
        self.requests: list[httpx.Request] = []
        self.handlers = handlers

        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            handler = self.handlers[request.url.host]
            return handler(request) if callable(handler) else handler

        monkeypatch.setattr(
            ai_providers,
            "_http_client",
            lambda timeout_s: httpx.AsyncClient(transport=httpx.MockTransport(handle)),
        )

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]


def _ok(content: str | dict) -> httpx.Response:
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def _keys(monkeypatch, *, groq=False, openrouter=False) -> None:
    if groq:
        monkeypatch.setattr(settings, "groq_api_key", "gq-key")
    if openrouter:
        monkeypatch.setattr(settings, "openrouter_api_key", "or-key")


# ── Configuration ────────────────────────────────────────────────────────
def test_the_layer_is_configured_by_any_one_provider(monkeypatch):
    assert ai.is_configured() is False
    _keys(monkeypatch, groq=True)
    assert ai.is_configured() is True


def test_a_provider_left_out_of_the_order_is_never_used_even_with_a_key(monkeypatch):
    _keys(monkeypatch, groq=True, openrouter=True)
    monkeypatch.setattr(settings, "ai_provider_order", "openrouter, bogus ,openrouter")
    assert ai_providers.provider_order() == ["openrouter"]
    assert ai_providers.configured_providers() == ["openrouter"]


# ── Falling through ──────────────────────────────────────────────────────
async def test_a_rate_limited_gemini_falls_through_to_groq(monkeypatch):
    gemini = _gemini(monkeypatch, _GeminiError(429, "RESOURCE_EXHAUSTED quota"))
    _keys(monkeypatch, groq=True)
    http = _Http(monkeypatch, {"api.groq.com": _ok(_answer())})

    result = await ai.understand_prescription(RAW_TEXT, EXISTING)

    assert result is not None and result.rows[0]["medicine"] == "Metformin"
    assert gemini.calls == 1
    assert http.hosts() == ["api.groq.com"]


async def test_groq_failing_too_falls_through_to_openrouter(monkeypatch):
    _gemini(monkeypatch, _GeminiError(503))
    _keys(monkeypatch, groq=True, openrouter=True)
    http = _Http(
        monkeypatch,
        {
            "api.groq.com": httpx.Response(500, json={"error": "boom"}),
            "openrouter.ai": _ok(_answer()),
        },
    )

    result = await ai.understand_prescription(RAW_TEXT, EXISTING)

    assert result is not None
    assert http.hosts() == ["api.groq.com", "openrouter.ai"]


async def test_every_provider_failing_returns_none_so_callers_use_the_deterministic_result(
    monkeypatch,
):
    _gemini(monkeypatch, _GeminiError(429))
    _keys(monkeypatch, groq=True, openrouter=True)
    _Http(monkeypatch, {"api.groq.com": httpx.Response(429), "openrouter.ai": httpx.Response(401)})

    assert await ai.understand_prescription(RAW_TEXT, EXISTING) is None


async def test_the_first_working_provider_wins_and_later_ones_are_not_called(monkeypatch):
    _gemini(monkeypatch, ai._GeminiPrescriptionResponse(**_answer()))
    _keys(monkeypatch, groq=True)
    http = _Http(monkeypatch, {"api.groq.com": _ok(_answer())})

    assert await ai.understand_prescription(RAW_TEXT, EXISTING) is not None
    assert http.requests == []


async def test_the_configured_order_is_respected(monkeypatch):
    gemini = _gemini(monkeypatch, ai._GeminiPrescriptionResponse(**_answer()))
    _keys(monkeypatch, groq=True)
    monkeypatch.setattr(settings, "ai_provider_order", "groq,gemini")
    http = _Http(monkeypatch, {"api.groq.com": _ok(_answer())})

    assert await ai.understand_prescription(RAW_TEXT, EXISTING) is not None
    assert http.hosts() == ["api.groq.com"] and gemini.calls == 0


# ── Cooldowns ────────────────────────────────────────────────────────────
async def test_an_exhausted_provider_is_skipped_on_the_next_request_not_retried(monkeypatch):
    """The point of the ledger: one failed call when the free tier runs out,
    not a fresh timeout on every upload for the rest of the day."""
    gemini = _gemini(monkeypatch, _GeminiError(429, "RESOURCE_EXHAUSTED"))
    _keys(monkeypatch, groq=True)
    _Http(monkeypatch, {"api.groq.com": _ok(_answer())})

    await ai.understand_prescription(RAW_TEXT, EXISTING)
    await ai.understand_prescription(RAW_TEXT, EXISTING)

    assert gemini.calls == 1  # the second request skipped Gemini entirely
    assert ai_providers.in_cooldown("gemini") is True


def test_the_cooldown_kind_follows_how_the_provider_failed(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider_cooldown_s", 60)
    assert ai_providers.note_failure("gemini", _GeminiError(429)) == "rate_limited"
    assert ai_providers.note_failure("groq", _GeminiError(401)) == "auth_or_model"
    assert ai_providers.note_failure("openrouter", TimeoutError()) == "timeout"
    ai_providers.reset_state()
    assert ai_providers.in_cooldown("gemini") is False


async def test_a_retry_after_header_sets_the_cooldown(monkeypatch):
    _keys(monkeypatch, groq=True)
    _Http(monkeypatch, {"api.groq.com": httpx.Response(429, headers={"retry-after": "123"})})
    monkeypatch.setattr(settings, "ai_provider_cooldown_s", 5)

    assert await ai.understand_prescription(RAW_TEXT, EXISTING) is None

    remaining = ai_providers._cooldown_until["groq"] - __import__("time").monotonic()
    assert 100 < remaining <= 123


async def test_the_chain_stops_once_the_total_time_budget_is_spent(monkeypatch):
    monkeypatch.setattr(settings, "ai_total_timeout_s", 0)
    _keys(monkeypatch, groq=True)
    http = _Http(monkeypatch, {"api.groq.com": _ok(_answer())})

    assert await ai.understand_prescription(RAW_TEXT, EXISTING) is None
    assert http.requests == []


# ── The OpenAI-compatible call itself ────────────────────────────────────
async def test_the_request_sends_text_only_with_the_key_and_the_schema(monkeypatch):
    _keys(monkeypatch, groq=True)
    http = _Http(monkeypatch, {"api.groq.com": _ok(_answer())})

    await ai.understand_prescription(RAW_TEXT, EXISTING)

    request = http.requests[0]
    body = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer gq-key"
    assert body["model"] == settings.groq_model
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0
    system, user = (m["content"] for m in body["messages"])
    assert "UNTRUSTED DOCUMENT CONTENT" in system  # the same safety instruction as Gemini
    assert "medicine_certainty" in system  # the schema is spelled out in the prompt
    assert RAW_TEXT in user
    assert set(body) == {"model", "temperature", "response_format", "messages"}  # no images


async def test_a_json_answer_wrapped_in_a_code_fence_is_still_accepted(monkeypatch):
    _keys(monkeypatch, groq=True)
    _Http(monkeypatch, {"api.groq.com": _ok("```json\n" + json.dumps(_answer()) + "\n```")})

    result = await ai.understand_prescription(RAW_TEXT, EXISTING)

    assert result is not None and result.rows[0]["medicine"] == "Metformin"


@pytest.mark.parametrize(
    "content",
    ["not json at all", '{"medicines": "nope"}', '{"medicines":[{"medicine_certainty":"maybe"}]}'],
)
async def test_an_unusable_answer_is_a_failure_not_a_result(monkeypatch, content):
    _keys(monkeypatch, groq=True, openrouter=True)
    http = _Http(monkeypatch, {"api.groq.com": _ok(content), "openrouter.ai": _ok(_answer())})

    result = await ai.understand_prescription(RAW_TEXT, EXISTING)

    assert result is not None  # the NEXT provider answered
    assert http.hosts() == ["api.groq.com", "openrouter.ai"]


# ── A fallback model is held to the same standard as Gemini ──────────────
async def test_a_fallback_providers_invented_medicine_is_downgraded_like_geminis(monkeypatch):
    _keys(monkeypatch, groq=True)
    _Http(monkeypatch, {"api.groq.com": _ok(_answer(medicine="Zorblaxitol"))})

    result = await ai.understand_prescription(RAW_TEXT, EXISTING)

    assert result is not None
    assert result.certainty[0]["medicine"] == "ambiguous"  # shares nothing with the source text


async def test_the_doctor_validation_path_uses_the_chain_too(monkeypatch):
    _gemini(monkeypatch, _GeminiError(429))
    _keys(monkeypatch, openrouter=True)
    _Http(monkeypatch, {"openrouter.ai": _ok(_answer())})

    result = await ai.validate_doctor_prescription(
        [
            {
                "medicine": "Metformin",
                "dosage": "500 mg",
                "frequency": "twice daily",
                "duration": "30 days",
            }
        ]
    )

    assert result is not None and result.certainty[0]["medicine"] == "high"


async def test_no_key_anywhere_means_no_call_and_no_result(monkeypatch):
    http = _Http(monkeypatch, {})
    assert await ai.understand_prescription(RAW_TEXT, EXISTING) is None
    assert http.requests == []


def test_nothing_secret_is_logged_on_failure(monkeypatch, capsys):
    _keys(monkeypatch, groq=True)
    ai_providers.note_failure(
        "groq",
        httpx.HTTPStatusError(
            "boom gq-key",
            request=httpx.Request("POST", "https://x"),
            response=httpx.Response(401),
        ),
    )
    out = capsys.readouterr().out
    assert "gq-key" not in out
    assert "groq" in out
