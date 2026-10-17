import uuid

import pytest
from pydantic import ValidationError

from app.schemas.chat import ChatRequest


def test_chat_request_accepts_message_and_class_id():
    class_id = uuid.uuid4()

    request = ChatRequest(message="  Explain this topic  ", class_id=class_id)

    assert request.message == "Explain this topic"
    assert request.class_id == class_id


@pytest.mark.parametrize("message", ["", " ", "   \n\t  "])
def test_chat_request_rejects_blank_message(message: str):
    with pytest.raises(ValidationError):
        ChatRequest(message=message, class_id=uuid.uuid4())


def test_chat_request_rejects_invalid_class_id():
    with pytest.raises(ValidationError):
        ChatRequest(message="A valid question", class_id="not-a-uuid")
