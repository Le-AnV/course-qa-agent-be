"""Unit tests for suggestion_service (AI-125)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from app.services import suggestion_service
from app.services.rag_service import ScoredChunk


def test_parse_json_questions_clean_list():
    raw = '["What is AI?", "Explain neural nets?", "How do weights update?"]'
    questions = suggestion_service._parse_json_questions(raw, max_count=3, min_count=2)
    assert questions == ["What is AI?", "Explain neural nets?", "How do weights update?"]


def test_parse_json_questions_with_markdown_fences():
    raw = """```json
    [
      "What is machine learning",
      "Why use deep learning?"
    ]
    ```"""
    questions = suggestion_service._parse_json_questions(raw, max_count=3, min_count=2)
    assert questions == ["What is machine learning?", "Why use deep learning?"]


def test_parse_json_questions_below_min_count():
    raw = '["Only one question?"]'
    questions = suggestion_service._parse_json_questions(raw, max_count=3, min_count=2)
    assert questions == []


def test_parse_json_questions_malformed():
    raw = "Not a json output at all"
    questions = suggestion_service._parse_json_questions(raw, max_count=3, min_count=2)
    assert questions == []


def test_generate_followup_questions_success():
    class FakeLLM:
        async def ainvoke(self, messages):
            return MagicMock(
                content='["What is backpropagation?", "How to choose a learning rate?"]'
            )

    chunks = [
        ScoredChunk(
            content="Neural networks use backpropagation to calculate gradients and update weights with a learning rate.",
            score=0.9,
            metadata={"file_name": "nn.pdf"},
        )
    ]

    questions = asyncio.run(
        suggestion_service.generate_followup_questions(
            question="What is a neural network?",
            answer="A neural network is a computational model composed of interconnected layers.",
            chunks=chunks,
            model=FakeLLM(),
        )
    )

    assert questions == ["What is backpropagation?", "How to choose a learning rate?"]


def test_generate_followup_questions_insufficient_context():
    # AC-03: Insufficient context (< 50 chars) returns []
    chunks = [
        ScoredChunk(
            content="Short info",
            score=0.9,
            metadata={"file_name": "nn.pdf"},
        )
    ]

    questions = asyncio.run(
        suggestion_service.generate_followup_questions(
            question="What is this?",
            answer="Answer",
            chunks=chunks,
        )
    )

    assert questions == []


def test_generate_followup_questions_handles_llm_failure():
    # AC-05: LLM error gracefully returns [] without throwing
    class FailingLLM:
        async def ainvoke(self, messages):
            raise RuntimeError("LLM service unavailable")

    chunks = [
        ScoredChunk(
            content="Neural networks use backpropagation to calculate gradients and update weights with a learning rate.",
            score=0.9,
            metadata={"file_name": "nn.pdf"},
        )
    ]

    questions = asyncio.run(
        suggestion_service.generate_followup_questions(
            question="What is a neural network?",
            answer="A neural network is a computational model composed of interconnected layers.",
            chunks=chunks,
            model=FailingLLM(),
        )
    )

    assert questions == []
