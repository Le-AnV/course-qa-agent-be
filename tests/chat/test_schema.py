"""Request validation for the AI-61 class-scoped chat endpoint."""

import uuid

import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatRequest


def test_chat_request_normalizes_message() -> None:
    class_id = uuid.uuid4()

    payload = ChatRequest(message="  What is a neural network?  ", class_id=class_id)

    assert payload.message == "What is a neural network?"
    assert payload.class_id == class_id


@pytest.mark.parametrize("message", ["", " ", "\n\t"])
def test_chat_request_rejects_blank_message(message: str) -> None:
    with pytest.raises(ValidationError):
        ChatRequest(message=message, class_id=uuid.uuid4())


def test_chat_request_rejects_invalid_class_id() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(message="What is unit 1 about?", class_id="not-a-uuid")
