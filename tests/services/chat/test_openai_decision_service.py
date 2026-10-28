from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
from pydantic import SecretStr

from app.services import openai_decision_service


def test_scope_decision_returns_true_for_confident_out_of_scope(monkeypatch) -> None:
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured["timeout"] = kwargs["timeout"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, body=json)
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "answers": [
                        {
                            "type": "choice",
                            "name": "scope",
                            "choice": "out_of_scope",
                            "confidence": 0.94,
                        }
                    ]
                },
            )

    monkeypatch.setattr(openai_decision_service.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(openai_decision_service.settings, "openai_api_key", SecretStr("secret"))

    result = asyncio.run(
        openai_decision_service.is_confidently_out_of_scope(
            class_name="C++ Basic",
            class_description="Introductory C++ course",
            question="How do Python lists work?",
            history="",
        )
    )

    assert result is True
    assert captured["url"] == "https://api.openai.com/v1/decisions"
    assert captured["headers"] == {"Authorization": "Bearer secret"}
    assert captured["body"]["model"] == "gpt-6-luna"
    state = json.loads(captured["body"]["input"])
    assert state["class_name"] == "C++ Basic"
    assert state["class_description"] == "Introductory C++ course"
    assert state["question"] == "How do Python lists work?"
    scope_question = captured["body"]["questions"][0]
    assert scope_question["type"] == "choice"
    assert "specific subject taught in this class" in scope_question["instructions"]
    assert "C++ Basic" in scope_question["instructions"]
    assert "Python is out_of_scope" in scope_question["instructions"]


def test_scope_decision_allows_uncertain_or_in_scope_result(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {
                    "answers": [
                        {
                            "type": "choice",
                            "name": "scope",
                            "choice": "out_of_scope",
                            "confidence": 0.79,
                        }
                    ]
                },
            )

    monkeypatch.setattr(openai_decision_service.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(openai_decision_service.settings, "openai_api_key", SecretStr("secret"))

    result = asyncio.run(
        openai_decision_service.is_confidently_out_of_scope(
            class_name="Python Programming",
            class_description=None,
            question="Can Python be used for data science?",
            history="",
        )
    )

    assert result is False


def test_scope_decision_is_disabled_without_openai_key(monkeypatch) -> None:
    monkeypatch.setattr(openai_decision_service.settings, "openai_api_key", None)

    result = asyncio.run(
        openai_decision_service.is_confidently_out_of_scope(
            class_name="Python Programming",
            class_description=None,
            question="What were today's lottery numbers?",
            history="",
        )
    )

    assert result is False


def test_scope_decision_fails_open_when_openai_is_unavailable(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("provider unavailable")

    monkeypatch.setattr(openai_decision_service.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(openai_decision_service.settings, "openai_api_key", SecretStr("secret"))

    result = asyncio.run(
        openai_decision_service.is_confidently_out_of_scope(
            class_name="Python Programming",
            class_description=None,
            question="What were today's lottery numbers?",
            history="",
        )
    )

    assert result is False
