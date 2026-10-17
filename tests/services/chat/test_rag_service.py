import asyncio
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document
from pydantic import SecretStr

from app.config.config import settings
from app.services import rag_service
from app.services.rag_service import SYSTEM_INSTRUCTION, build_citations, build_prompt


def test_build_citations_preserves_prompt_order_and_source_locations():
    pdf_id = uuid.uuid4()
    docx_id = uuid.uuid4()

    citations = build_citations(
        [
            Document(
                page_content="A* expands the node with the lowest estimated total cost.",
                metadata={
                    "document_id": str(pdf_id),
                    "file_name": "Introduction to AI.pdf",
                    "file_type": ".pdf",
                    "chapter": "Chapter 4",
                    "headers": {"Header 2": "Informed Search"},
                    "page_number": 37,
                },
            ),
            Document(
                page_content="The assignment asks students to compare search strategies.",
                metadata={
                    "document_id": str(docx_id),
                    "file_name": "Assignment 2.docx",
                    "file_type": ".docx",
                    "headers": {"Header 1": "Requirements"},
                },
            ),
        ]
    )

    assert citations == [
        {
            "marker": "[1]",
            "document_id": str(pdf_id),
            "file_name": "Introduction to AI.pdf",
            "heading": "Chapter 4 > Informed Search",
            "page": 37,
            "url": f"/api/documents/{pdf_id}/content?page=37",
        },
        {
            "marker": "[2]",
            "document_id": str(docx_id),
            "file_name": "Assignment 2.docx",
            "heading": "Requirements",
            "page": None,
            "url": f"/api/documents/{docx_id}/content",
        },
    ]


def test_build_prompt_labels_retrieved_sources_and_limits_context_to_documents():
    prompt = build_prompt(
        "Where does the course explain recursion?",
        [
            Document(
                page_content="Recursion is a function calling itself.",
                metadata={"file_name": "Lecture 2.pdf", "page_number": 4},
            ),
            Document(
                page_content="The base case stops recursive calls.",
                metadata={"file_name": "Lecture 3.pdf", "headers": {"Header 1": "Functions"}},
            ),
        ],
    )

    assert "Where does the course explain recursion?" in prompt
    assert "[1] Lecture 2.pdf, page 4" in prompt
    assert "Recursion is a function calling itself." in prompt
    assert "[2] Lecture 3.pdf, Functions" in prompt
    assert "The base case stops recursive calls." in prompt
    assert "Base factual claims only on the retrieved materials" in SYSTEM_INSTRUCTION
    assert "[3]" not in prompt


def test_build_prompt_tells_model_when_no_context_was_retrieved():
    prompt = build_prompt("What is the final exam date?", [])

    assert "No relevant course materials were retrieved." in prompt


def test_stream_answer_returns_a_grounded_fallback_without_calling_gemini(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        rag_service.genai,
        "Client",
        lambda **_kwargs: pytest.fail("Gemini must not be called without retrieved context"),
    )

    async def collect() -> list[str]:
        return [text async for text in rag_service.stream_answer("Unknown topic", [])]

    assert asyncio.run(collect()) == [
        "The available course materials do not contain enough information to answer this question."
    ]


def test_stream_answer_uses_grounded_prompt_and_streams_gemini_chunks(
    monkeypatch: pytest.MonkeyPatch,
):
    captured = {}

    class FakeAsyncClient:
        models = SimpleNamespace()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def chunks() -> AsyncIterator[object]:
        yield SimpleNamespace(text="Based on the lecture")
        yield SimpleNamespace(text=" [1].")

    async def generate_content_stream(**kwargs):
        captured.update(kwargs)
        return chunks()

    FakeAsyncClient.models.generate_content_stream = generate_content_stream
    fake_client = SimpleNamespace(aio=FakeAsyncClient(), close=lambda: None)
    monkeypatch.setattr(rag_service.genai, "Client", lambda **_kwargs: fake_client)
    monkeypatch.setattr(settings, "google_api_key", SecretStr("test-key"))

    async def collect() -> list[str]:
        return [
            text
            async for text in rag_service.stream_answer(
                "Explain recursion",
                [
                    Document(
                        page_content="Recursion calls itself", metadata={"file_name": "Lecture.pdf"}
                    )
                ],
            )
        ]

    result = asyncio.run(collect())

    assert result == ["Based on the lecture", " [1]."]
    assert captured["model"] == settings.llm_model
    assert "Recursion calls itself" in captured["contents"]
    assert captured["config"].system_instruction == SYSTEM_INSTRUCTION
