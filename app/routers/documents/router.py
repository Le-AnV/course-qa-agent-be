"""Documents router: upload / list / approve / reject / versions (AI-35/37/41).

Auth (AI-25/27): all endpoints require a Bearer JWT for a trainer or admin
via ``app.core.deps.require_trainer``. Identity is the DB ``User`` row.

Plan v3 (course-shared documents): documents belong to a course, so every
trainer of that course may review them. An out-of-course document is a 404
(never leak existence); admins bypass all scope checks.
"""

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_trainer
from app.models.course import Class
from app.models.document import Document, DocumentVersion
from app.models.user import User
from app.schemas.documents import (
    AcceptedTypeRead,
    ClassMineRead,
    CourseClassRead,
    DocumentEventListResponse,
    DocumentEventRead,
    DocumentListResponse,
    DocumentRead,
    DocumentRejectRequest,
    DocumentRollbackRequest,
    DocumentUploadResponse,
    DocumentUploadResult,
    DocumentVersionListResponse,
    DocumentVersionRead,
    MyCourseRead,
)
from app.services import document_service
from app.services.document_service import ACCEPTED_EXTENSIONS, VALID_STATUSES

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _record_to_read(
    db: Session, record: Document, *, removed_class_id: uuid.UUID | None = None
) -> DocumentRead:
    return DocumentRead(
        id=str(record.id),
        file_name=record.file_name,
        file_type=record.file_type,
        mime_type=record.mime_type,
        size_bytes=record.size_bytes,
        uploader_id=str(record.uploaded_by),
        class_id=str(record.class_id) if record.class_id else None,
        class_name=document_service.class_name(db, record.class_id),
        scope=record.scope,
        status=record.status,
        review_note=record.review_note,
        created_at=record.created_at,
        course_id=str(record.course_id) if record.course_id else None,
        version_no=document_service.latest_version_no(db, record.id),
        current_version_no=document_service.version_number(db, record.current_version_id),
        created_by=str(record.created_by) if record.created_by else None,
        needs_review=record.needs_review,
        upload_warning=getattr(record, "upload_warning", None),
        removed_for_class=document_service.is_removed_for_class(
            db, record.id, removed_class_id
        ),
    )


def _version_to_read(
    version: DocumentVersion, current_version_id
) -> DocumentVersionRead:
    return DocumentVersionRead(
        id=str(version.id),
        version_no=version.version_no,
        status=version.status,
        file_hash=version.file_hash,
        review_note=version.review_note,
        uploaded_by=str(version.uploaded_by) if version.uploaded_by else None,
        reviewed_by=str(version.reviewed_by) if version.reviewed_by else None,
        reviewed_at=version.reviewed_at,
        created_at=version.created_at,
        is_current=version.id == current_version_id,
    )


def _load_visible(db: Session, user: User, document_id: str) -> Document:
    """Return a document the caller may see, else 404 (D6: no existence leak)."""

    record = document_service.get_document(db, document_id)
    if record is None or record.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if not document_service.can_view_document(db, user, record):
        raise HTTPException(status_code=404, detail="Document not found.")
    return record


def _load_reviewable(db: Session, user: User, document_id: str) -> Document:
    """Return a document the caller may review (D2), else 404/403 (D6)."""

    record = document_service.get_document(db, document_id)
    if record is None or record.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if document_service.can_review_document(db, user, record):
        return record
    if document_service.can_view_document(db, user, record):
        raise HTTPException(status_code=403, detail="Review is not allowed for your role.")
    raise HTTPException(status_code=404, detail="Document not found.")


@router.get("/accepted-types", response_model=list[AcceptedTypeRead])
def accepted_types(
    _user: User = Depends(require_trainer),
) -> list[AcceptedTypeRead]:
    """Every accepted file type for the upload screen (Upload AC-07)."""
    labels = {
        ".pdf": "PDF document",
        ".docx": "Word document",
        ".md": "Markdown",
        ".txt": "Plain text",
        ".pptx": "PowerPoint",
        ".xlsx": "Excel",
    }
    return [
        AcceptedTypeRead(extension=ext, mime=mime, label=labels[ext])
        for ext, mime in sorted(ACCEPTED_EXTENSIONS.items())
    ]


@router.get("/classes/mine", response_model=list[ClassMineRead])
def classes_mine(
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> list[ClassMineRead]:
    """Classes assigned to the caller (Upload AC-05 / List AC-05)."""
    return [ClassMineRead(**c) for c in document_service.my_classes(db, user)]


@router.get("/courses/mine", response_model=list[MyCourseRead])
def courses_mine(
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> list[MyCourseRead]:
    """Courses assigned to the caller (teacher Course list)."""
    return [MyCourseRead(**c) for c in document_service.my_courses(db, user)]


@router.post("/", response_model=DocumentUploadResponse, status_code=201)
async def upload_documents(
    files: list[UploadFile] = File(...),
    course_id: str = Form(default=""),
    class_id: str = Form(default=""),
    scope: str = Form(default="course"),
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentUploadResponse:
    """Multi-file material upload (Upload AC-04); each file validated.

    ``scope="course"`` (default) uploads material shared with every class of
    ``course_id``. ``scope="class"`` uploads material private to ``class_id``
    (the class page). ``class_id`` is optional for course uploads (a label).
    """
    if scope not in document_service.VALID_SCOPES:
        raise HTTPException(status_code=400, detail=f"Unknown scope '{scope}'.")
    results: list[DocumentUploadResult] = []
    for upload in files:
        content = await upload.read()
        name = upload.filename or "unnamed"
        try:
            record = document_service.create_document(
                db,
                name,
                content,
                class_id or None,
                user,
                course_id=course_id or None,
                scope=scope,
            )
        except ValueError as exc:
            results.append(DocumentUploadResult(file_name=name, error=str(exc)))
            continue
        results.append(
            DocumentUploadResult(
                file_name=name,
                document=_record_to_read(db, record),
                warning=getattr(record, "upload_warning", None),
            )
        )
    return DocumentUploadResponse(uploaded=results)


@router.get("/", response_model=DocumentListResponse)
def get_documents(
    search: str = Query(default=""),
    class_id: str | None = Query(default=None),
    course_id: str | None = Query(default=None),
    file_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    scope: str | None = Query(default=None),
    include_removed: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentListResponse:
    """Browse/search/filter/paginate the caller's visible documents (List AC-01..05).

    With a ``class_id`` filter, ``include_removed=true`` also returns documents
    the class removed from its own list (``removed_for_class``), for Restore.
    ``scope`` narrows to subject-wide (``"course"``) or class-only materials.
    """
    if status is not None and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Unknown status '{status}'.")
    if scope is not None and scope not in document_service.VALID_SCOPES:
        raise HTTPException(status_code=400, detail=f"Unknown scope '{scope}'.")
    rows, total = document_service.list_documents(
        db,
        user,
        search=search,
        class_id=class_id,
        course_id=course_id,
        file_type=file_type,
        status=status,
        scope=scope,
        page=page,
        page_size=page_size,
        include_removed=include_removed,
    )
    removed_class_id: uuid.UUID | None = None
    if class_id is not None:
        try:
            removed_class_id = uuid.UUID(str(class_id))
        except (TypeError, ValueError, AttributeError):
            removed_class_id = None
    return DocumentListResponse(
        items=[
            _record_to_read(db, r, removed_class_id=removed_class_id) for r in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{document_id}/versions", response_model=DocumentVersionListResponse)
def get_document_versions(
    document_id: str,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentVersionListResponse:
    """Full version history, newest first (plan v3 D4). Readable course-wide."""

    record = _load_visible(db, user, document_id)
    versions = document_service.versions_for(db, record.id)
    return DocumentVersionListResponse(
        items=[_version_to_read(v, record.current_version_id) for v in versions]
    )


@router.post("/{document_id}/approve", response_model=DocumentRead)
def approve_document(
    document_id: str,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentRead:
    """Approve pending -> processing and atomically switch the serving version.

    Plan v3 D2: every trainer of the document's course may approve (self-review
    included); admins bypass. AI-41 originally said trainer-only (TA gets 403).
    """
    record = _load_reviewable(db, user, document_id)
    try:
        record = document_service.approve_document(db, record.id, reviewer=user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _record_to_read(db, record)


@router.post("/{document_id}/reject", response_model=DocumentRead)
def reject_document(
    document_id: str,
    payload: DocumentRejectRequest | None = None,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentRead:
    """Reject pending -> rejected (terminal). Paired with approve (plan v3 Phase 1).

    Same guards as approve: 404 outside the course / unknown id, 400 non-pending.
    Self-review is allowed (D2); the audit trail is the uploader + reviewer.
    """

    record = _load_reviewable(db, user, document_id)
    try:
        record = document_service.reject_document(
            db,
            record.id,
            reason=payload.reason if payload else None,
            reviewer=user,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _record_to_read(db, record)


def _event_to_read(event) -> DocumentEventRead:
    return DocumentEventRead(
        id=str(event.id),
        version_no=event.version_no,
        action=event.action,
        actor_id=str(event.actor_id) if event.actor_id else None,
        detail=event.detail,
        created_at=event.created_at,
    )


@router.post("/{document_id}/rollback", response_model=DocumentRead)
def rollback_document(
    document_id: str,
    payload: DocumentRollbackRequest,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentRead:
    """Serve an older version again; re-indexes it first (plan v3 Phase 2c)."""

    record = _load_reviewable(db, user, document_id)
    try:
        record = document_service.rollback_document(
            db, record.id, payload.version_no, reviewer=user
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Version not found.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _record_to_read(db, record)


@router.post("/{document_id}/retry", response_model=DocumentRead)
def retry_document(
    document_id: str,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentRead:
    """Retry the newest failed version (plan v3 Phase 2c)."""

    record = _load_reviewable(db, user, document_id)
    try:
        record = document_service.retry_document(db, record.id, reviewer=user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _record_to_read(db, record)


@router.get("/courses/{course_id}/classes", response_model=list[CourseClassRead])
def course_classes(
    course_id: str,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> list[CourseClassRead]:
    """Classes of a course (Course page)."""

    try:
        course_uuid = uuid.UUID(str(course_id))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="Course not found.")
    if not document_service.can_access_course(db, user, course_uuid):
        raise HTTPException(status_code=404, detail="Course not found.")
    classes = db.query(Class).filter(Class.course_id == course_uuid).order_by(Class.code).all()
    return [CourseClassRead(id=str(c.id), code=c.code, name=c.name) for c in classes]


@router.delete("/{document_id}/classes/{class_id}", response_model=DocumentRead)
def remove_from_class(
    document_id: str,
    class_id: str,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentRead:
    """Remove a document from one class only (per-class opt-out, AI-37).

    The document stays in the subject: every other class keeps seeing it.
    Any trainer of the course (or an admin) may do this; ``PUT`` restores it.
    """

    record = _load_reviewable(db, user, document_id)
    try:
        record = document_service.remove_from_class(db, record.id, class_id, actor=user)
    except KeyError:
        raise HTTPException(status_code=404, detail="Class not found for this document.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _record_to_read(db, record, removed_class_id=uuid.UUID(str(class_id)))


@router.put("/{document_id}/classes/{class_id}", response_model=DocumentRead)
def restore_to_class(
    document_id: str,
    class_id: str,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentRead:
    """Bring a document back to a class that had removed it (undo opt-out)."""

    record = _load_reviewable(db, user, document_id)
    try:
        record = document_service.restore_to_class(db, record.id, class_id, actor=user)
    except KeyError:
        raise HTTPException(status_code=404, detail="Class not found for this document.")
    return _record_to_read(db, record, removed_class_id=uuid.UUID(str(class_id)))


@router.delete("/{document_id}", response_model=DocumentRead)
def delete_document(
    document_id: str,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentRead:
    """Soft-delete a document for the whole course (uploader or admin only)."""

    record = _load_visible(db, user, document_id)
    try:
        record = document_service.delete_document(db, record.id, user)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return _record_to_read(db, record)


@router.get("/{document_id}/events", response_model=DocumentEventListResponse)
def list_events(
    document_id: str,
    user: User = Depends(require_trainer),
    db: Session = Depends(get_db),
) -> DocumentEventListResponse:
    """Full audit trail of a document, newest first (plan v3 Phase 2c)."""

    record = _load_visible(db, user, document_id)
    return DocumentEventListResponse(
        items=[_event_to_read(e) for e in document_service.events_for(db, record.id)]
    )
