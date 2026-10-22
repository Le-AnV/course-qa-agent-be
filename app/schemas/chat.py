"""Request schemas for the class-scoped chat API (AI-61)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, field_validator


class ChatRequest(BaseModel):
    """One question scoped to a specific class."""

    message: str
    class_id: uuid.UUID

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Message must not be empty.")
        return cleaned
