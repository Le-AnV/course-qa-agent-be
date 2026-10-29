"""Streaming behavior for the AI-61 chat service."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services import (
    chat_retrieval_service,
    chat_service,
    rag_service,
    suggestion_service,
)
from app.services.chat_service import stream_chat
from app.services.llm import gateway


def _parse_event(raw: str) -> tuple[str, dict]:
    lines = raw.strip().splitlines()
    assert lines[0].startswith("event: ")
    assert lines[1].startswith("data: ")
    return lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))


async def _collect(stream) -> list[tuple[str, dict]]:
    return [_parse_event(item) async for item in stream]


@pytest.fixture(autouse=True)
def _disable_scope_gate_for_existing_chat_tests(monkeypatch) -> None:
    async def in_scope(**kwargs) -> bool:
        return False

    monkeypatch.setattr(
        chat_service.decision_service,
        "is_confidently_out_of_scope",
        in_scope,
    )
    monkeypatch.setattr(chat_service.course_service, "get_class", lambda *args: None)


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
        "progress",
        "token",
        "token",
        "citation",
        "done",
    ]
    assert [data["stage"] for event, data in events if event == "progress"] == [
        "searching",
        "searching",
        "reading",
        "drafting",
    ]
    assert events[4][1] == {"text": "Neural networks "}
    assert events[6][1] == {
        "index": 1,
        "kind": "curriculum",
        "title": "lesson.pdf",
        "url": None,
        "snippet": "A neural network contains connected layers.",
        "document_id": "doc-1",
        "page": 2,
        "indexes": [1],
        "highlights": ["A neural network contains connected layers."],
    }
    assert events[7][1] == {
        "source": "curriculum",
        "used_web_search": False,
        "threshold": 0.8,
        "top_score": 0.91,
        "session_id": None,
        "user_message_id": None,
        "assistant_message_id": None,
        "suggestions": [],
        "suggested_questions": [],
    }
    rendered_prompt = "\n".join(str(message.content) for message in captured_messages)
    assert "A neural network contains connected layers." in rendered_prompt
    assert "Explain neural networks." in rendered_prompt
    assert "Every factual claim must end with the matching source marker" in rendered_prompt


def test_confident_out_of_scope_skips_retrieval(monkeypatch) -> None:
    scope_arguments: dict = {}

    async def out_of_scope(**kwargs) -> bool:
        scope_arguments.update(kwargs)
        return True

    monkeypatch.setattr(
        chat_service.decision_service,
        "is_confidently_out_of_scope",
        out_of_scope,
    )
    monkeypatch.setattr(
        chat_service.course_service,
        "get_class",
        lambda *args: SimpleNamespace(
            name="Python Programming",
            info=SimpleNamespace(description="Introductory Python course"),
        ),
    )
    monkeypatch.setattr(
        chat_retrieval_service,
        "retrieve_for_chat",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("Out-of-scope questions must not reach retrieval")
        ),
    )

    events = asyncio.run(
        _collect(
            stream_chat(
                db=MagicMock(),
                message="What were today's lottery numbers?",
                class_id="class-a",
                timeout_seconds=1,
            )
        )
    )

    assert [event for event, _ in events] == ["progress", "token", "done"]
    assert events[0][1] == {
        "stage": "searching",
        "message": "Checking class scope",
    }
    assert "outside the scope" in events[1][1]["text"]
    assert events[2][1]["source"] == rag_service.SOURCE_UNRELATED
    assert events[2][1]["used_web_search"] is False
    assert scope_arguments["class_name"] == "Python Programming"
    assert scope_arguments["class_description"] == "Introductory Python course"
    assert scope_arguments["question"] == "What were today's lottery numbers?"


def test_stream_chat_emits_restart_and_keeps_only_the_new_answer(monkeypatch) -> None:
    _patch_curriculum(monkeypatch)

    async def fake_answer_stream(db, model, messages):
        yield "partial "
        yield gateway.StreamRestart()
        yield "complete answer [1]."

    monkeypatch.setattr("app.services.chat_service._answer_stream", fake_answer_stream)

    events = asyncio.run(
        _collect(
            stream_chat(
                db=MagicMock(),
                message="Explain neural networks.",
                class_id="class-a",
                model=object(),
                timeout_seconds=1,
            )
        )
    )

    assert ("restart", {"message": "Switching to the next Gemini key."}) in events
    assert [data["text"] for event, data in events if event == "token"] == [
        "partial ",
        "complete answer [1].",
    ]
    assert events[-1][0] == "done"


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
        ("progress", {"stage": "searching", "message": "Checking class scope"}),
        ("progress", {"stage": "searching", "message": "Searching class materials"}),
        ("progress", {"stage": "reading", "message": "Found 1 section in lesson.pdf"}),
        ("progress", {"stage": "drafting", "message": "Synthesizing answer"}),
        (
            "error",
            {"message": "The answer could not be generated. Please try again."},
        ),
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
        ("progress", {"stage": "searching", "message": "Checking class scope"}),
        ("progress", {"stage": "searching", "message": "Searching class materials"}),
        ("progress", {"stage": "reading", "message": "Found 1 section in lesson.pdf"}),
        ("progress", {"stage": "drafting", "message": "Synthesizing answer"}),
        (
            "error",
            {"message": "The answer timed out. Please try again."},
        ),
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

    assert [event for event, _ in events] == ["progress", "progress", "token", "done"]
    assert events[0][1] == {"stage": "searching", "message": "Checking class scope"}
    assert events[1][1] == {"stage": "searching", "message": "Searching class materials"}
    assert events[2][1] == {"text": chat_retrieval_service.NO_DOCUMENT_EVIDENCE_MESSAGE}
    assert events[3][1]["source"] == chat_retrieval_service.SOURCE_NONE
    assert events[3][1]["used_web_search"] is False


def test_stream_chat_emits_suggestions_event_and_done(monkeypatch) -> None:
    """AI-125 AC-01: stream_chat emits suggestions event and populates done metadata."""

    class ModelWithSuggestions:
        async def astream(self, messages):
            yield SimpleNamespace(
                content="Neural networks consist of multiple interconnected layers [1]."
            )

        async def ainvoke(self, messages):
            return SimpleNamespace(
                content='["How does backpropagation update weights?", "What activation functions are commonly used?"]'
            )

    _patch_curriculum(monkeypatch)

    events = asyncio.run(
        _collect(
            stream_chat(
                db=MagicMock(),
                message="Explain neural networks.",
                class_id="class-a",
                model=ModelWithSuggestions(),
                timeout_seconds=2,
            )
        )
    )

    event_names = [event for event, _ in events]
    assert "suggestions" in event_names
    assert "done" in event_names

    # The unified pipeline includes suggestions in the final metadata.
    assert event_names.index("suggestions") < event_names.index("done")

    suggestions_event = next(data for event, data in events if event == "suggestions")
    assert suggestions_event == {
        "questions": [
            "How does backpropagation update weights?",
            "What activation functions are commonly used?",
        ]
    }

    done_event = next(data for event, data in events if event == "done")
    assert done_event["suggestions"] == suggestions_event["questions"]


def test_stream_chat_generates_suggestions_after_the_answer_completes(monkeypatch) -> None:
    started = asyncio.Event()
    captured: dict = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        started.set()
        return ["Question one?", "Question two?"]

    monkeypatch.setattr(
        suggestion_service, "generate_followup_questions", fake_generate
    )
    _patch_curriculum(monkeypatch)

    class Model:
        async def astream(self, messages):
            assert not started.is_set()
            yield SimpleNamespace(content="Answer [1].")

    events = asyncio.run(
        _collect(
            stream_chat(
                db=MagicMock(),
                message="Explain neural networks.",
                class_id="class-a",
                model=Model(),
                timeout_seconds=2,
            )
        )
    )

    names = [event for event, _ in events]
    assert names.index("suggestions") < names.index("done")
    assert captured["answer"] == "Answer [1]."
    assert captured["question"] == "Explain neural networks."
    assert captured["history"] == chat_service.chat_context_service.format_history_context([])
