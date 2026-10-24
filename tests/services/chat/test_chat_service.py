"""Streaming behavior for the AI-61 chat service."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.services import chat_retrieval_service, rag_service
from app.services.chat_service import stream_chat


def _parse_event(raw: str) -> tuple[str, dict]:
    lines = raw.strip().splitlines()
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: ")
    return lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))


async def _collect(stream) -> list[tuple[str, dict]]:
    return [_parse_event(item) async for item in stream]


def _curriculum_decision() -> rag_service.RetrievalDecision:
    return rag_service.RetrievalDecision(
        question="Explain neural networks.",
        class_id="class-a",
        threshold=0.8,
        top_score=0.91,
        source=rag_service.SOURCE_CURRICULUM,
        chunks=(
            rag_service.ScoredChunk(
                content="A neural network contains connected layers.",
                score=0.91,
                metadata={
                    "document_id": "doc-1",
                    "file_name": "lesson.pdf",
                    "page_number": 2,
                },
            ),
        ),
    )


def _patch_curriculum(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_retrieval_service,
        "retrieve_for_chat",
        lambda *args, **kwargs: _curriculum_decision(),
    )

def test_stream_chat_emits_tokens_citations_and_done(monkeypatch) -> None:
    captured_messages: list[object] = []

    class FakeModel:
        async def astream(self, messages):
            captured_messages.extend(messages)
            yield SimpleNamespace(content="Neural networks ")
            yield SimpleNamespace(content="contain layers [1].")

    _patch_curriculum(monkeypatch)

    events = asyncio.run(
        _collect(
            stream_chat(
                db=MagicMock(),
                message="Explain neural networks.",
                class_id="class-a",
                model=FakeModel(),
                timeout_seconds=1,
            )
        )
    )

    assert [event for event, _ in events] == [
        "progress",
        "progress",
        "progress",
        "token",
        "token",
        "citation",
        "done",
    ]
    assert [data["stage"] for event, data in events if event == "progress"] == [
        "searching",
        "reading",
        "drafting",
    ]
    assert events[3][1] == {"text": "Neural networks "}
    assert events[5][1] == {
        "index": 1,
        "kind": "curriculum",
        "title": "lesson.pdf",
        "url": None,
        "snippet": "A neural network contains connected layers.",
        "document_id": "doc-1",
        "page": 2,
    }
    assert events[6][1] == {
        "source": "curriculum",
        "used_web_search": False,
        "threshold": 0.8,
        "top_score": 0.91,
        "session_id": None,
        "user_message_id": None,
        "assistant_message_id": None,
    }
    rendered_prompt = "\n".join(str(message.content) for message in captured_messages)
    assert "A neural network contains connected layers." in rendered_prompt
    assert "Explain neural networks." in rendered_prompt
    assert "Every factual claim must end with the matching source marker" in rendered_prompt


def test_stream_chat_uses_evidence_even_when_cosine_is_below_08(monkeypatch) -> None:
    """Water Cycle regression: a relevant chunk below 0.8 must still be evaluated."""

    decision = rag_service.RetrievalDecision(
        question="What are the main stages of the water cycle?",
        class_id="class-a",
        threshold=0.8,
        top_score=0.774,
        source=rag_service.SOURCE_CURRICULUM,
        chunks=(
            rag_service.ScoredChunk(
                content="Water evaporates, condenses, and returns as precipitation.",
                score=0.774,
                metadata={
                    "document_id": "water-cycle",
                    "file_name": "The Water Cycle TG.pdf",
                    "page_number": 1,
                },
            ),
        ),
    )

    class FakeModel:
        async def astream(self, messages):
            yield SimpleNamespace(content="Evaporation, condensation, and precipitation [1].")

    monkeypatch.setattr(
        chat_retrieval_service,
        "retrieve_for_chat",
        lambda *args, **kwargs: decision,
    )
    events = asyncio.run(
        _collect(
            stream_chat(
                db=MagicMock(),
                message="What are the main stages of the water cycle?",
                class_id="class-a",
                model=FakeModel(),
                timeout_seconds=1,
            )
        )
    )

    assert [event for event, _ in events] == [
        "progress",
        "progress",
        "progress",
        "token",
        "citation",
        "done",
    ]
    assert events[-1][1]["source"] == rag_service.SOURCE_CURRICULUM
    assert events[-1][1]["top_score"] == 0.774


def test_stream_chat_converts_model_failure_to_sse_error(monkeypatch) -> None:
    class BrokenModel:
        async def astream(self, messages):
            raise RuntimeError("secret provider detail")
            yield  # pragma: no cover - keeps this an async generator

    _patch_curriculum(monkeypatch)

    events = asyncio.run(
        _collect(
            stream_chat(
                db=MagicMock(),
                message="Explain neural networks.",
                class_id="class-a",
                model=BrokenModel(),
                timeout_seconds=1,
            )
        )
    )

    assert events == [
        ("progress", {"stage": "searching"}),
        ("progress", {"stage": "reading"}),
        ("progress", {"stage": "drafting"}),
        (
            "error",
            {"message": "The answer could not be generated. Please try again."},
        )
    ]


def test_stream_chat_times_out_without_hanging(monkeypatch, caplog) -> None:
    class SlowModel:
        async def astream(self, messages):
            await asyncio.sleep(1)
            yield SimpleNamespace(content="too late")

    _patch_curriculum(monkeypatch)

    events = asyncio.run(
        _collect(
            stream_chat(
                db=MagicMock(),
                message="Explain neural networks.",
                class_id="class-a",
                model=SlowModel(),
                    timeout_seconds=0.5,
            )
        )
    )

    assert events == [
        ("progress", {"stage": "searching"}),
        ("progress", {"stage": "reading"}),
        ("progress", {"stage": "drafting"}),
        (
            "error",
            {"message": "The answer timed out. Please try again."},
        )
    ]
    assert "timed out during grounded answer generation" in caplog.text


def _no_source_decision() -> rag_service.RetrievalDecision:
    return rag_service.RetrievalDecision(
        question="What is a transformer?",
        class_id="class-a",
        threshold=0.8,
        top_score=0.2,
        source=chat_retrieval_service.SOURCE_NONE,
    )


def test_stream_chat_does_not_search_web_or_call_model_without_evidence(monkeypatch) -> None:
    """AI-61 answers only from class documents and fails closed without evidence."""

    class ExplodingModel:
        async def astream(self, messages):
            raise AssertionError("The model must not be called without a source")
            yield  # pragma: no cover - keeps this an async generator

    monkeypatch.setattr(
        chat_retrieval_service,
        "retrieve_for_chat",
        lambda *args, **kwargs: _no_source_decision(),
    )
    monkeypatch.setattr(
        rag_service,
        "search_internet",
        lambda question: (_ for _ in ()).throw(AssertionError("AI-61 must not search the web")),
    )

    events = asyncio.run(
        _collect(
            stream_chat(
                db=MagicMock(),
                message="What is a transformer?",
                class_id="class-a",
                model=ExplodingModel(),
                timeout_seconds=1,
            )
        )
    )

    assert [event for event, _ in events] == ["progress", "token", "done"]
    assert events[0][1] == {"stage": "searching"}
    assert events[1][1] == {"text": chat_retrieval_service.NO_DOCUMENT_EVIDENCE_MESSAGE}
    assert events[2][1]["source"] == chat_retrieval_service.SOURCE_NONE
    assert events[2][1]["used_web_search"] is False
