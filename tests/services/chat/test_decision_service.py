"""Class scope decision: provider selection, structured output, fail-open."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Generator
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from app.services import decision_service


@pytest.fixture(autouse=True)
def _clear_decision_cooldown() -> Generator[None, None, None]:
    decision_service.reset_failure_state()
    yield
    decision_service.reset_failure_state()


def _class_question():
    return decision_service.is_confidently_out_of_scope(
        class_name="C++ Basic",
        class_description="Introductory C++ course",
        question="How do Python lists work?",
        history="Student: what is a pointer\nAssistant: a pointer holds an address",
    )


def _use_gemini(
    monkeypatch,
    *,
    parsed=None,
    text: str | None = None,
    error: Exception | None = None,
    captured: dict | None = None,
) -> None:
    captured = captured if captured is not None else {}

    class FakeModels:
        async def generate_content(self, **kwargs):
            captured["call"] = kwargs
            if error is not None:
                raise error
            return SimpleNamespace(parsed=parsed, text=text)

    class FakeAio:
        def __init__(self) -> None:
            self.models = FakeModels()
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured["client"] = kwargs
            self.aio = FakeAio()

    monkeypatch.setattr(decision_service.genai, "Client", FakeClient)
    monkeypatch.setattr(decision_service.settings, "decision_provider", "gemini")
    monkeypatch.setattr(decision_service.settings, "google_api_key", SecretStr("secret"))


def _use_gemini_sequence(monkeypatch, outcomes: list[object]) -> dict:
    """Answer each Gemini attempt from a scripted list of outcomes."""

    captured: dict[str, list] = {"calls": [], "clients": []}

    class FakeModels:
        async def generate_content(self, **kwargs):
            captured["calls"].append(kwargs)
            outcome = outcomes[len(captured["calls"]) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return SimpleNamespace(parsed=outcome, text=None)

    class FakeAio:
        def __init__(self) -> None:
            self.models = FakeModels()

        async def aclose(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured["clients"].append(kwargs)
            self.aio = FakeAio()

    monkeypatch.setattr(decision_service.genai, "Client", FakeClient)
    monkeypatch.setattr(decision_service.settings, "decision_provider", "gemini")
    monkeypatch.setattr(decision_service.settings, "google_api_key", SecretStr("secret"))
    return captured


def test_gemini_blocks_a_confident_out_of_scope(monkeypatch) -> None:
    captured: dict = {}
    _use_gemini(
        monkeypatch,
        parsed=decision_service.ScopeDecision(verdict="out_of_scope", confidence=0.94),
        captured=captured,
    )

    assert asyncio.run(_class_question()) is True

    call = captured["call"]
    assert call["model"] == decision_service.settings.decision_model
    config = call["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema is decision_service.ScopeDecision
    assert config.max_output_tokens == decision_service.settings.decision_max_output_tokens
    assert "syllabus" in config.system_instruction
    assert "C++ Basic" in call["contents"]
    assert "Introductory C++ course" in call["contents"]
    assert "How do Python lists work?" in call["contents"]
    assert "a pointer holds an address" in call["contents"]
    assert captured["client"]["api_key"] == "secret"
    expected_timeout = decision_service._primary_timeout(
        decision_service.settings.decision_timeout_seconds,
        decision_service.settings.decision_fallback_model,
    )
    # The SDK refuses a manual deadline below 10s; asyncio.wait_for enforces
    # the real (possibly shorter) per-attempt budget.
    assert captured["client"]["http_options"].timeout == max(10_000, int(expected_timeout * 1000))
    assert captured["client"]["http_options"].retry_options.attempts == 1


def test_gemini_allows_an_in_scope_question(monkeypatch) -> None:
    _use_gemini(
        monkeypatch,
        parsed=decision_service.ScopeDecision(verdict="in_scope", confidence=0.97),
    )

    assert asyncio.run(_class_question()) is False


def test_gemini_allows_an_uncertain_negative_verdict(monkeypatch) -> None:
    _use_gemini(
        monkeypatch,
        parsed=decision_service.ScopeDecision(verdict="out_of_scope", confidence=0.55),
    )

    assert asyncio.run(_class_question()) is False


def test_gemini_honours_the_configured_threshold(monkeypatch) -> None:
    _use_gemini(
        monkeypatch,
        parsed=decision_service.ScopeDecision(verdict="out_of_scope", confidence=0.9),
    )
    monkeypatch.setattr(
        decision_service.settings,
        "decision_confidence_threshold",
        0.95,
    )

    assert asyncio.run(_class_question()) is False


def test_gemini_parses_a_text_payload_when_parsed_is_empty(monkeypatch) -> None:
    _use_gemini(
        monkeypatch,
        parsed=None,
        text=json.dumps({"verdict": "out_of_scope", "confidence": 0.91}),
    )

    assert asyncio.run(_class_question()) is True


def test_gemini_is_disabled_without_a_key(monkeypatch) -> None:
    class ExplodingClient:
        def __init__(self, **kwargs) -> None:
            raise AssertionError("no client may be built without a key")

    monkeypatch.setattr(decision_service.genai, "Client", ExplodingClient)
    monkeypatch.setattr(decision_service.settings, "decision_provider", "gemini")
    monkeypatch.setattr(decision_service.settings, "google_api_key", None)

    assert asyncio.run(_class_question()) is False


def test_gemini_fails_open_when_the_provider_errors(monkeypatch) -> None:
    _use_gemini(monkeypatch, error=RuntimeError("provider down"))

    assert asyncio.run(_class_question()) is False


def test_gemini_fails_open_on_a_malformed_payload(monkeypatch) -> None:
    _use_gemini(monkeypatch, parsed=None, text="not json")

    assert asyncio.run(_class_question()) is False


def test_gemini_fails_open_on_an_unknown_verdict(monkeypatch) -> None:
    _use_gemini(
        monkeypatch,
        parsed=None,
        text=json.dumps({"verdict": "maybe", "confidence": 0.99}),
    )

    assert asyncio.run(_class_question()) is False


def test_gemini_uses_the_fallback_model_after_a_primary_failure(monkeypatch) -> None:
    captured = _use_gemini_sequence(
        monkeypatch,
        [
            RuntimeError("503 UNAVAILABLE"),
            decision_service.ScopeDecision(verdict="out_of_scope", confidence=0.93),
        ],
    )

    assert asyncio.run(_class_question()) is True

    models = [call["model"] for call in captured["calls"]]
    assert models == [
        decision_service.settings.decision_model,
        decision_service.settings.decision_fallback_model,
    ]
    assert decision_service._cooldown_remaining() > 0


def test_gemini_skips_a_primary_model_in_cooldown(monkeypatch) -> None:
    captured = _use_gemini_sequence(
        monkeypatch,
        [decision_service.ScopeDecision(verdict="in_scope", confidence=0.9)],
    )
    decision_service._start_cooldown()

    assert asyncio.run(_class_question()) is False

    models = [call["model"] for call in captured["calls"]]
    assert models == [decision_service.settings.decision_fallback_model]


def test_gemini_fails_open_when_primary_and_fallback_fail(monkeypatch) -> None:
    captured = _use_gemini_sequence(
        monkeypatch,
        [RuntimeError("503 UNAVAILABLE"), TimeoutError("fallback slow")],
    )

    assert asyncio.run(_class_question()) is False
    assert len(captured["calls"]) == 2


def test_fallback_can_be_disabled(monkeypatch) -> None:
    captured = _use_gemini_sequence(monkeypatch, [RuntimeError("503 UNAVAILABLE")])
    monkeypatch.setattr(decision_service.settings, "decision_fallback_enabled", False)

    assert asyncio.run(_class_question()) is False
    assert len(captured["calls"]) == 1


def test_primary_budget_reserves_time_for_the_fallback(monkeypatch) -> None:
    captured = _use_gemini_sequence(
        monkeypatch,
        [decision_service.ScopeDecision(verdict="in_scope", confidence=0.9)],
    )

    asyncio.run(_class_question())

    expected = decision_service._primary_timeout(
        decision_service.settings.decision_timeout_seconds,
        decision_service.settings.decision_fallback_model,
    )
    assert expected < decision_service.settings.decision_timeout_seconds
    assert captured["clients"][0]["http_options"].timeout == max(10_000, int(expected * 1000))


def test_cooldown_expiry_retries_the_primary_model(monkeypatch) -> None:
    captured = _use_gemini_sequence(
        monkeypatch,
        [decision_service.ScopeDecision(verdict="in_scope", confidence=0.9)],
    )
    monkeypatch.setattr(
        decision_service,
        "_primary_cooldown_until",
        time.monotonic() - 0.01,
    )

    assert asyncio.run(_class_question()) is False

    models = [call["model"] for call in captured["calls"]]
    assert models == [decision_service.settings.decision_model]
    assert decision_service._cooldown_remaining() == 0


def test_gemini_client_never_goes_below_the_sdk_minimum_deadline(monkeypatch) -> None:
    captured = _use_gemini_sequence(
        monkeypatch,
        [decision_service.ScopeDecision(verdict="in_scope", confidence=0.9)],
    )

    asyncio.run(_class_question())

    # Gemini rejects a manual HTTP deadline below 10s with 400 INVALID_ARGUMENT.
    assert captured["clients"][0]["http_options"].timeout >= 10_000
