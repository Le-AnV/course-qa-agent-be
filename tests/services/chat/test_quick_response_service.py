"""Unit tests for quick_response_service."""

from __future__ import annotations

import pytest

from app.services.quick_response_service import (
    get_quick_response,
    split_into_stream_chunks,
)


@pytest.mark.parametrize(
    "msg",
    [
        "xin chào",
        "Chào bạn!",
        "hello",
        "hi bot",
        "good morning",
        "hé lô",
        "alo ad",
    ],
)
def test_greetings_matched(msg: str) -> None:
    resp = get_quick_response(msg)
    assert resp is not None
    assert len(resp) > 0


@pytest.mark.parametrize(
    "msg",
    [
        "bạn là ai",
        "who are you",
        "what can you do",
        "bạn có thể làm gì",
        "hướng dẫn",
        "help",
        "hệ thống này là gì",
    ],
)
def test_identity_and_help_matched(msg: str) -> None:
    resp = get_quick_response(msg)
    assert resp is not None
    assert len(resp) > 0


@pytest.mark.parametrize(
    "msg",
    [
        "cảm ơn bạn",
        "thanks a lot",
        "thank you",
        "ok cảm ơn bot",
        "tks",
    ],
)
def test_gratitude_matched(msg: str) -> None:
    resp = get_quick_response(msg)
    assert resp is not None
    assert len(resp) > 0


@pytest.mark.parametrize(
    "msg",
    [
        "tạm biệt",
        "bye bye",
        "goodbye",
        "hẹn gặp lại",
    ],
)
def test_farewell_matched(msg: str) -> None:
    resp = get_quick_response(msg)
    assert resp is not None
    assert len(resp) > 0


@pytest.mark.parametrize(
    "msg",
    [
        "Chào bạn, cho mình hỏi thuật toán A* hoạt động như thế nào?",
        "Explain neural networks.",
        "What is a transformer model?",
        "Tài liệu tuần 3 nằm ở đâu vậy bot?",
        "Hello, how can I calculate gradient descent in assignment 2?",
    ],
)
def test_real_course_inquiries_not_intercepted(msg: str) -> None:
    # Must fail closed to pass to normal RAG search
    resp = get_quick_response(msg)
    assert resp is None


def test_split_into_stream_chunks() -> None:
    text = "Xin chào bạn mình là trợ lý AI học tập"
    chunks = split_into_stream_chunks(text, chunk_size=3)
    assert "".join(chunks) == text
