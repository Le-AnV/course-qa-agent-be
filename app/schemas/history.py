"""Pydantic schemas for Chat History module (AI-95, Epic E7)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ChatRole = Literal["USER", "ASSISTANT", "SYSTEM"]


class ChatCitation(BaseModel):
    """Citation snapshot stored with an assistant answer (AI-63 AC-03)."""

    index: int
    kind: str
    title: str
    url: str | None = None
    snippet: str = ""
    document_id: str | None = None
    page: int | None = None


class ChatMessageBase(BaseModel):
    """Base schema for chat message attributes."""

    role: ChatRole = Field(description="Role of the message sender (USER, ASSISTANT, SYSTEM)")
    content: str = Field(min_length=1, description="Text content of the message")
    used_web_search: bool = Field(
        default=False, description="Whether web search was utilized for generating the answer"
    )
    citations: list[ChatCitation] = Field(
        default_factory=list,
        description="Sources cited by this message, preserved across history reloads",
    )

    @field_validator("citations", mode="before")
    @classmethod
    def normalize_citations(cls, value):
        """Treat a legacy NULL column as an empty citation list."""

        return value or []


class MessageCreateRequest(ChatMessageBase):
    """Request payload to append a message to an existing session."""



class ChatMessageResponse(ChatMessageBase):
    """Response payload for a single chat message (AC-02)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID
    created_at: datetime


class SessionCreateRequest(BaseModel):
    """Request payload to explicitly create a new chat session (AC-03)."""

    class_id: uuid.UUID = Field(description="UUID of the class the session belongs to")
    title: str | None = Field(default=None, max_length=255, description="Optional conversation title")


class SessionRenameRequest(BaseModel):
    """Request payload to rename an owned chat session."""

    title: str = Field(min_length=1, max_length=255, description="New conversation title")

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        """Reject blank titles and store the trimmed value."""

        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Title must not be empty.")
        return cleaned


class SessionPinRequest(BaseModel):
    """Request payload to toggle or explicitly set session pin state."""

    is_pinned: bool | None = Field(
        default=None,
        description="Explicit boolean to set pin status. If omitted, current state is toggled.",
    )


class ChatSessionResponse(BaseModel):
    """Response schema for chat session summary in sidebar/list (AC-04, AC-06)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    class_id: uuid.UUID
    title: str | None
    created_at: datetime
    updated_at: datetime
    is_pinned: bool = False
    pinned_at: datetime | None = None


class ChatSessionListResponse(BaseModel):
    """Response schema for list of chat sessions."""

    items: list[ChatSessionResponse] = Field(default_factory=list)
    total: int = 0


class ChatDetailResponse(ChatSessionResponse):
    """Detailed response schema for a session including all ordered messages (AC-05, AC-07)."""

    messages: list[ChatMessageResponse] = Field(default_factory=list)


class DeleteSessionResponse(BaseModel):
    """Response schema for session deletion confirmation."""

    message: str = "Chat session deleted successfully."


class SearchResultItem(BaseModel):
    """Search result item representing a matching chat session (AC-01, AC-03)."""

    session_id: uuid.UUID
    title: str | None
    updated_at: datetime
    snippet: str
    match_in: Literal["title", "message"]


class ChatSearchResponse(BaseModel):
    """Response schema for GET /api/history/search endpoint (AI-101)."""

    items: list[SearchResultItem] = Field(default_factory=list)
    total: int = 0
    query: str

