"""Pydantic schemas for Epic E2: documents (AI-35/37/41)."""

from datetime import datetime

from pydantic import BaseModel, Field


class AcceptedTypeRead(BaseModel):
    extension: str
    mime: str
    label: str


class DocumentRead(BaseModel):
    """List row: file name, type, size, uploader, class, upload date (AC-01)."""

    id: str
    file_name: str
    file_type: str
    mime_type: str
    size_bytes: int
    uploader_id: str
    class_id: str
    class_name: str
    status: str
    created_at: datetime


class DocumentUploadResult(BaseModel):
    """One result per file in a multi-file upload (AC-04)."""

    file_name: str
    document: DocumentRead | None = None
    error: str | None = None


class DocumentUploadResponse(BaseModel):
    uploaded: list[DocumentUploadResult] = Field(default_factory=list)
