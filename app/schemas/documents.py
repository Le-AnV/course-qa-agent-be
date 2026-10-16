"""Pydantic schemas for Epic E2: documents (AI-35/37/41)."""

from datetime import datetime

from pydantic import BaseModel, Field


class ClassMineRead(BaseModel):
    """A class assigned to the caller (source: E1 when it lands)."""

    id: str
    course_id: str
    name: str
    code: str


class MyCourseRead(BaseModel):
    """A course the caller is assigned to (teacher Course list)."""

    id: str
    code: str
    name: str
    class_count: int = 0


class CourseClassRead(BaseModel):
    """One class of a course (Course page)."""

    id: str
    code: str
    name: str


class AcceptedTypeRead(BaseModel):
    extension: str
    mime: str
    label: str


class DocumentRead(BaseModel):
    """List/upload row (AC-01) plus the Phase 2b versioning fields."""

    id: str
    file_name: str
    file_type: str
    mime_type: str
    size_bytes: int
    uploader_id: str
    class_id: str | None = None  # owning class for class-only materials, else null
    class_name: str = ""
    scope: str = "course"  # "course" = shared with the subject; "class" = class-only
    status: str
    review_note: str | None = None
    created_at: datetime
    # Course-scoped documents + versioning.
    course_id: str | None = None
    version_no: int | None = None  # newest uploaded version
    current_version_no: int | None = None  # serving version (None until approved)
    created_by: str | None = None  # v1 uploader (D7)
    needs_review: bool = False
    upload_warning: str | None = None  # duplicate hash / name clash, upload only
    removed_for_class: bool = False  # per-class opt-out (only with a class filter)


class DocumentVersionRead(BaseModel):
    """One row of a document's version history (plan v3 D4)."""

    id: str
    version_no: int
    status: str
    file_hash: str | None = None
    review_note: str | None = None
    uploaded_by: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime
    is_current: bool = False


class DocumentVersionListResponse(BaseModel):
    items: list[DocumentVersionRead] = Field(default_factory=list)


class DocumentRejectRequest(BaseModel):
    """Optional reviewer note when rejecting a pending document (plan v3 Phase 1)."""

    reason: str | None = Field(default=None, max_length=500)


class DocumentRollbackRequest(BaseModel):
    """Rollback target (plan v3 Phase 2c)."""

    version_no: int = Field(ge=1)


class DocumentEventRead(BaseModel):
    """One audit-trail row (plan v3 Phase 2c)."""

    id: str
    version_no: int | None = None
    action: str
    actor_id: str | None = None
    detail: str | None = None
    created_at: datetime


class DocumentEventListResponse(BaseModel):
    items: list[DocumentEventRead] = Field(default_factory=list)


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
