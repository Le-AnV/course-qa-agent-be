"""Pydantic schemas for Epic E2: documents (AI-35/37/41)."""

from datetime import datetime

from pydantic import BaseModel, Field


class ClassMineRead(BaseModel):
    """A class assigned to the caller (source: E1 when it lands)."""

    id: str
    name: str
    code: str


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
    failure_reason: str | None = None
    version: int = 1
    created_at: datetime


class DocumentDeleteResponse(BaseModel):
    """AI-39 AC-01: the file is gone and its chunks were purged from Qdrant."""

    deleted: str
    file_name: str
    version: int
    purged_chunks: int = 0


class DocumentUploadResult(BaseModel):
    """One result per file in a multi-file upload (AC-04)."""

    file_name: str
    document: DocumentRead | None = None
    error: str | None = None
    warning: str | None = None


class DocumentUploadResponse(BaseModel):
    uploaded: list[DocumentUploadResult] = Field(default_factory=list)


class DocumentListResponse(BaseModel):
    items: list[DocumentRead] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 20
