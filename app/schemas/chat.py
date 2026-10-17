"""Request schemas for contextual chat."""

import uuid

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    """A question scoped to one class the caller is allowed to access."""

    message: str = Field(min_length=1)
    class_id: uuid.UUID

    @field_validator("message")
    @classmethod
    def strip_and_require_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message must not be empty.")
        return value
