"""Admin endpoints — courses, classes, accounts, imports, email (AI-31, AI-105). Prefix: /api/admin."""

from __future__ import annotations

import io
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_db, require_admin
from app.models.course import Class, Enrollment
from app.models.email_outbox import EmailOutbox
from app.models.import_job import ROW_FAILED, ImportBatch, ImportRow
from app.models.user import User
from app.routers.admin.gemini import router as gemini_router
from app.schemas.admin import (
    AdminSettingsRead,
    ClassAssign,
    ClassCreate,
    ClassPrerequisiteRead,
    ClassPrerequisiteUpdate,
    ClassRead,
    ClassStatusUpdate,
    ClassTrainerAdd,
    ClassTrainerRead,
    ConnectionTestResult,
    CourseCreate,
    CourseRead,
    EmailConfigRead,
    EmailConfigUpdate,
    EmailOutboxRead,
    EmailQueueStats,
    ImportBatchDetail,
    ImportBatchRead,
    ImportRowRead,
    RetrievalConfigRead,
    RetrievalConfigUpdate,
    RetrievalTestRequest,
    RetrievalTestResponse,
    UserCodeGenerate,
    UserCodeRead,
    UserCreate,
    UserDeactivate,
    UserRead,
)
from app.schemas.system_health import SystemHealthRead
from app.services import (
    chat_retrieval_service,
    course_service,
    email_service,
    import_service,
    rag_service,
    user_service,
)
from app.services.admin_settings_service import build_admin_settings
from app.services.llm import (
    LlmConfigurationError,
    LlmConnectionError,
    check_llm_connection,
)
from app.services.system_health_service import build_health

router = APIRouter(prefix="/api/admin", tags=["admin"])
router.include_router(gemini_router)


def _class_to_read(db: Session, section: Class) -> ClassRead:
    warnings = course_service.class_warnings(db, section.id)
    return ClassRead(
        id=section.id,
        code=section.code,
        trainer_id=section.trainer_id,
        trainer_name=course_service.user_display_name(db, section.trainer_id),
        trainers=[
            ClassTrainerRead(**t) for t in course_service.list_trainers(db, section.id)
        ],
        credits=section.credits or 0,
        student_count=course_service.count_students(db, section.id),
        capacity=course_service.get_config(db, section.id)["capacity"],
        status=section.status or course_service.STATUS_ACTIVE,
        document_count=warnings["document_count"],
        ticket_count=warnings["ticket_count"],
        prerequisites=[
            ClassPrerequisiteRead(id=p.id, code=p.code, name=p.name)
            for p in course_service.class_prerequisites(db, section.id)
        ],
    )


def _user_to_read(db: Session, user: User) -> UserRead:
    # ERD: no ``User.class_id``. Class scope = trainer slot or enrollment.
    class_id: uuid.UUID | None = None
    class_code: str | None = None
    # Primary trainer OR co-trainer: both count as "teaching this class".
    trainer_class_ids = course_service.class_ids_for_trainer(db, user.id)
    owned = (
        db.query(Class)
        .filter(Class.id.in_(trainer_class_ids))
        .order_by(Class.code)
        .first()
        if trainer_class_ids
        else None
    )
    if owned is not None:
        class_id, class_code = owned.id, owned.code
    else:
        enrolled = (
            db.query(Enrollment)
            .filter(Enrollment.student_id == user.id, Enrollment.status == "ACTIVE")
            .first()
        )
        if enrolled is not None:
            section = course_service.get_class(db, enrolled.class_id)
            if section is not None:
                class_id, class_code = section.id, section.code
    role_name = user_service.role_name_of(user)
    if not role_name:
        role_name = user_service.normalize_role(user_service.ROLE_STUDENT)
    return UserRead(
        id=user.id,
        username=user.full_name,
        email=user.email,
        role=role_name.lower(),
        class_id=class_id,
        class_code=class_code,
        student_code=user.student_code,
        staff_code=user.staff_code,
        is_active=user.status == "ACTIVE",
    )


def _row_to_read(row: ImportRow) -> ImportRowRead:
    return ImportRowRead(
        id=row.id,
        row_number=row.row_number,
        username=row.username,
        email=row.email,
        role_raw=row.role_raw,
        class_raw=row.class_raw,
        status=row.status,
        message=row.message,
        student_code=row.student_code,
        staff_code=row.staff_code,
    )


# --- Courses (AC-01) ---
@router.post("/courses", response_model=CourseRead, status_code=status.HTTP_201_CREATED)
def create_course(payload: CourseCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    try:
        course = course_service.create_course(db, payload.code, payload.title)
    except course_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return CourseRead(id=course.id, code=course.code, title=course.name, class_count=0)


@router.get("/courses", response_model=list[CourseRead])
def list_courses(db: Session = Depends(get_db), _=Depends(require_admin)):
    # Legacy catalog: courses are decoupled from classes (class IS the subject),
    # so per-course class counts are always 0.
    return [
        CourseRead(id=course.id, code=course.code, title=course.name, class_count=0)
        for course in course_service.list_courses(db)
    ]


# --- Classes (standalone: class IS the subject) ---
@router.post("/classes", response_model=ClassRead, status_code=status.HTTP_201_CREATED)
def create_class(payload: ClassCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    try:
        section = course_service.create_class(
            db,
            payload.code,
            trainer_id=payload.trainer_id,
            name=payload.name,
            semester=payload.semester,
            academic_year=payload.academic_year,
            credits=payload.credits,
            capacity=payload.capacity,
            prerequisite_class_ids=payload.prerequisite_class_ids,
        )
    except course_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _class_to_read(db, section)


@router.get("/classes", response_model=list[ClassRead])
def list_classes(
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    return [_class_to_read(db, s) for s in course_service.list_classes(db)]


@router.patch("/classes/{class_id}/prerequisites", response_model=ClassRead)
def set_class_prerequisites(
    class_id: uuid.UUID,
    payload: ClassPrerequisiteUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Replace the subjects a student must pass before joining this class."""
    try:
        course_service.set_class_prerequisites(
            db, class_id, payload.prerequisite_class_ids
        )
    except course_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _class_to_read(db, course_service.get_class(db, class_id))


@router.patch("/classes/{class_id}/assign", response_model=ClassRead)
def assign_staff(
    class_id: uuid.UUID,
    payload: ClassAssign,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    try:
        section = course_service.assign_staff(db, class_id, payload.trainer_id)
    except course_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _class_to_read(db, section)


@router.post("/classes/{class_id}/trainers", response_model=ClassRead)
def add_class_trainer(
    class_id: uuid.UUID,
    payload: ClassTrainerAdd,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Add a co-trainer (teacher A starts the term, teacher B can take over)."""
    try:
        course_service.add_trainer(db, class_id, payload.trainer_id)
    except course_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _class_to_read(db, course_service.get_class(db, class_id))


@router.delete("/classes/{class_id}/trainers/{trainer_id}", response_model=ClassRead)
def remove_class_trainer(
    class_id: uuid.UUID,
    trainer_id: uuid.UUID,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Remove a co-trainer; the primary trainer is changed via /assign."""
    try:
        course_service.remove_trainer(db, class_id, trainer_id)
    except course_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _class_to_read(db, course_service.get_class(db, class_id))


@router.patch("/classes/{class_id}/status", response_model=ClassRead)
def set_class_status(
    class_id: uuid.UUID,
    payload: ClassStatusUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """P7: archive a class instead of deleting it; ACTIVE restores it."""
    try:
        section = course_service.set_class_status(db, class_id, payload.status)
    except course_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _class_to_read(db, section)


@router.delete("/classes/{class_id}")
def delete_class(
    class_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(require_admin)
):
    try:
        warnings = course_service.delete_class(db, class_id)
    except course_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return {"deleted": class_id, **warnings}


# --- Accounts (AC-03, AC-11, AC-12) ---
@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, db: Session = Depends(get_db), _=Depends(require_admin)):
    try:
        user, plain = user_service.create_user(
            db, payload.username, str(payload.email), payload.role, payload.class_code,
            student_code=payload.student_code,
            staff_code=payload.staff_code,
        )
    except user_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    # AC-07 pattern for single creates too: email the password, never return
    # it. Queued for the rate-limited drain (AC-09), not sent inline.
    subject, body = email_service.credential_email_body(
        user.full_name, user.email, plain, payload.class_code
    )
    email_service.queue_email(db, user.email, subject, body, user_id=user.id)
    return _user_to_read(db, user)


@router.post("/users/generate-code", response_model=UserCodeRead)
def generate_user_code(
    payload: UserCodeGenerate, db: Session = Depends(get_db), _=Depends(require_admin)
):
    try:
        role = user_service.normalize_role(payload.role)
        if role == user_service.ROLE_STUDENT:
            code = user_service.generate_student_code(db, payload.full_name)
        elif role in (user_service.ROLE_TRAINER, user_service.ROLE_TA):
            code = user_service.generate_staff_code(db, payload.full_name)
        else:
            raise user_service.DomainError(
                "Codes can only be generated for student, trainer or teaching assistant accounts."
            )
    except user_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return UserCodeRead(code=code)


@router.get("/users", response_model=list[UserRead])
def list_users(
    role: str | None = Query(default=None),
    class_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    return [_user_to_read(db, u) for u in user_service.list_users(db, role=role, class_id=class_id)]


@router.patch("/users/{user_id}/active", response_model=UserRead)
def set_user_active(
    user_id: uuid.UUID,
    payload: UserDeactivate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    try:
        user = user_service.set_active(db, user_id, payload.is_active)
    except user_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return _user_to_read(db, user)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: uuid.UUID, db: Session = Depends(get_db), _=Depends(require_admin)
):
    try:
        user, plain = user_service.reset_password(db, user_id)
    except user_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    subject, body = email_service.reset_email_body(user.full_name, user.email, plain)
    email_service.queue_email(db, user.email, subject, body, user_id=user.id)
    # Queued for the rate-limited drain (AC-09), not sent inline.
    return {"user_id": user.id, "emailed_to": user.email}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: uuid.UUID,
    confirm: bool = Query(default=False),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    if not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Deleting an account requires confirmation (?confirm=true).",
        )
    try:
        user_service.delete_user(db, user_id)
    except user_service.DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"deleted": user_id}


# --- Import (AC-05..AC-10) ---
@router.get("/import-template")
def download_template(_=Depends(require_admin)):
    data = import_service.build_template_bytes()
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=account-import-template.xlsx"},
    )


@router.post("/imports", response_model=ImportBatchRead, status_code=status.HTTP_201_CREATED)
async def upload_import(
    file: UploadFile, db: Session = Depends(get_db), admin=Depends(require_admin)
):
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Only .xlsx files are accepted."
        )
    data = await file.read()
    try:
        _, rows = import_service.parse_workbook(data)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    batch = import_service.import_rows(db, file.filename, rows, created_by_id=admin.id)
    return ImportBatchRead(
        id=batch.id,
        filename=batch.filename,
        total_rows=batch.total_rows,
        imported_count=batch.imported_count,
        skipped_count=batch.skipped_count,
        failed_count=batch.failed_count,
        created_at=batch.created_at,
    )


@router.get("/imports", response_model=list[ImportBatchRead])
def list_imports(db: Session = Depends(get_db), _=Depends(require_admin)):
    batches = db.query(ImportBatch).order_by(ImportBatch.id.desc()).all()
    return [
        ImportBatchRead(
            id=b.id,
            filename=b.filename,
            total_rows=b.total_rows,
            imported_count=b.imported_count,
            skipped_count=b.skipped_count,
            failed_count=b.failed_count,
            created_at=b.created_at,
        )
        for b in batches
    ]


@router.get("/imports/{batch_id}", response_model=ImportBatchDetail)
def get_import(batch_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    batch = db.query(ImportBatch).filter(ImportBatch.id == batch_id).first()
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import batch not found.")
    rows = (
        db.query(ImportRow)
        .filter(ImportRow.batch_id == batch_id)
        .order_by(ImportRow.row_number)
        .all()
    )
    return ImportBatchDetail(
        id=batch.id,
        filename=batch.filename,
        total_rows=batch.total_rows,
        imported_count=batch.imported_count,
        skipped_count=batch.skipped_count,
        failed_count=batch.failed_count,
        created_at=batch.created_at,
        rows=[_row_to_read(r) for r in rows],
    )


@router.post("/import-rows/{row_id}/retry")
def retry_import_row(row_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    row = db.query(ImportRow).filter(ImportRow.id == row_id).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import row not found.")
    if row.status != ROW_FAILED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only failed rows can be retried.")
    try:
        updated = import_service.retry_row(db, row_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return _row_to_read(updated)


# --- Email (AC-08, AC-09) ---
@router.get("/email-config", response_model=EmailConfigRead)
def get_email_config(db: Session = Depends(get_db), _=Depends(require_admin)):
    cfg = email_service.effective_email_config(db)
    return EmailConfigRead(**cfg)


@router.patch("/email-config", response_model=EmailConfigRead)
def update_email_config(
    payload: EmailConfigUpdate, db: Session = Depends(get_db), _=Depends(require_admin)
):
    cfg = email_service.save_email_config(db, payload.model_dump())
    return EmailConfigRead(**cfg)


@router.get("/email-queue/stats", response_model=EmailQueueStats)
def email_stats(db: Session = Depends(get_db), _=Depends(require_admin)):
    return EmailQueueStats(**email_service.queue_stats(db))


@router.get("/email-queue", response_model=list[EmailOutboxRead])
def list_email_queue(
    status_filter: str | None = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    q = db.query(EmailOutbox)
    if status_filter:
        q = q.filter(EmailOutbox.status == status_filter)
    q = q.order_by(EmailOutbox.id.desc()).limit(200)
    return [
        EmailOutboxRead(
            id=e.id,
            to_email=e.to_email,
            subject=e.subject,
            status=e.status,
            attempts=e.attempts,
            last_error=e.last_error,
            created_at=e.created_at,
            sent_at=e.sent_at,
        )
        for e in q.all()
    ]


@router.post("/email-queue/process")
def process_email_queue(db: Session = Depends(get_db), _=Depends(require_admin)):
    return email_service.process_queue(db)


@router.post("/email-queue/{email_id}/retry")
def retry_email(email_id: int, db: Session = Depends(get_db), _=Depends(require_admin)):
    try:
        entry = email_service.retry_failed(db, email_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return {"id": entry.id, "status": entry.status}


@router.get("/roles")
def list_roles(_=Depends(require_admin)):
    return {"roles": import_service.valid_role_names()}


# --- AI-105 runtime settings + connection checks (same DB-backed admin guard) ---
@router.get("/settings", response_model=AdminSettingsRead)
def read_admin_settings(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Return active configuration metadata without exposing credentials."""

    return build_admin_settings(settings, db)


@router.post("/llm/test-connection", response_model=ConnectionTestResult)
def test_llm_connection(_=Depends(require_admin)):
    """Check the configured Gemini key and model, returning a safe result."""

    try:
        return check_llm_connection(settings)
    except (LlmConfigurationError, LlmConnectionError) as error:
        return ConnectionTestResult(success=False, message=str(error))


# --- AI-65 retrieval score threshold (T35: admin-configurable, not hardcoded) ---
@router.get("/retrieval-config", response_model=RetrievalConfigRead)
def read_retrieval_config(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Return the active KB-vs-Internet threshold and its documented default."""

    return RetrievalConfigRead(**rag_service.retrieval_threshold_config(db))


@router.patch("/retrieval-config", response_model=RetrievalConfigRead)
def update_retrieval_config(
    payload: RetrievalConfigUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Persist a new threshold; the next question uses it (AI-65 AC-05)."""

    try:
        rag_service.save_retrieval_threshold(db, payload.threshold, payload.unrelated_threshold)
    except rag_service.RetrievalThresholdError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    return RetrievalConfigRead(**rag_service.retrieval_threshold_config(db))


@router.post("/retrieval-config/test", response_model=RetrievalTestResponse)
async def test_retrieval_query(
    payload: RetrievalTestRequest,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Test a sample query against class documents and return the raw top score and matched evidence."""
    target_class = None
    if payload.class_id:
        target_class = db.query(Class).filter(Class.id == payload.class_id).first()
    if target_class is None:
        target_class = db.query(Class).first()

    if target_class is None:
        return RetrievalTestResponse(
            question=payload.question,
            top_score=None,
            class_id=None,
            class_name="No classes found",
            matched_document=None,
            matched_snippet=None,
            source="none",
            search_query_en=None,
        )

    search_query_en = None
    try:
        from app.models.course import ClassInfo
        from app.services import decision_service

        info = db.query(ClassInfo).filter(ClassInfo.class_id == target_class.id).first()
        class_description = info.description if info else None

        scope_decision = await decision_service.decide_scope(
            class_name=target_class.name or target_class.code,
            class_description=class_description,
            question=payload.question,
            db=db,
        )
        if scope_decision and scope_decision.search_query_en:
            search_query_en = scope_decision.search_query_en
    except Exception:  # noqa: S110, BLE001
        pass

    decision = chat_retrieval_service.retrieve_for_chat(
        db,
        question=payload.question,
        class_id=target_class.id,
        search_query_en=search_query_en,
    )
    top_chunk = decision.chunks[0] if decision.chunks else None
    return RetrievalTestResponse(
        question=payload.question,
        top_score=round(decision.top_score, 4) if decision.top_score is not None else None,
        class_id=target_class.id,
        class_name=target_class.name or target_class.code,
        matched_document=top_chunk.title if top_chunk else None,
        matched_snippet=top_chunk.content[:240] if top_chunk else None,
        source=decision.source,
        search_query_en=search_query_en,
    )


# --- AI-109 health check & system monitoring (admin dashboard) ---
@router.get("/system/health", response_model=SystemHealthRead)
def system_health(_=Depends(require_admin), db: Session = Depends(get_db)):
    """Live component health (DB, Qdrant, Gemini API) for the admin dashboard.

    Unlike the fast public ``/health``, this performs a live LLM probe whose
    result is cached for a short TTL so dashboard refreshes stay cheap.
    """

    return build_health(db, settings, live_llm=True)
