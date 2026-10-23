"""Class configuration — capacity (P5) and typed teaching config (P6).

Prefix: /api/classes. The teaching fields are writable by admins and by the
trainers of that class (primary or co-trainer); the roster capacity is
admin-only — trainers read it but cannot change it. Students never see this
surface. Each class has one configuration row, and any class' configuration
can be copied onto another as a reusable template.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.course import Class
from app.models.user import User
from app.schemas.admin import (
    ClassConfigCopy,
    ClassConfigRead,
    ClassConfigSourceRead,
    ClassConfigUpdate,
    ClassInfoRead,
    ClassInfoUpdate,
    ClassTemplateCopy,
    ClassTemplateCopyRead,
    TemplateDocumentRead,
    TemplateDocumentSkip,
)
from app.services import course_service, document_service, user_service
from app.services.course_service import DomainError

router = APIRouter(prefix="/api/classes", tags=["class-config"])


def _require_class_teacher(db: Session, user: User, class_id: uuid.UUID) -> Class:
    """Admins, the primary trainer, or a co-trainer of this class."""
    section = course_service.get_class(db, class_id)
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")
    role = (user_service.role_name_of(user) or "").upper()
    if role == "ADMIN":
        return section
    if role == "TRAINER" and section.id in course_service.class_ids_for_trainer(db, user.id):
        return section
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only an admin or one of this class' trainers can change its configuration.",
    )


def _require_template_source(
    db: Session, user: User, source_class_id: uuid.UUID, target_id: uuid.UUID
) -> Class:
    """A source the caller taught (archived included) or any class for admins."""
    source = course_service.get_class(db, source_class_id)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The class you are copying from does not exist.",
        )
    if source.id == target_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pick a different class to copy the template from.",
        )
    role = (user_service.role_name_of(user) or "").upper()
    if role != "ADMIN" and source.id not in course_service.class_ids_for_trainer(db, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only copy a template from a class you teach.",
        )
    return source


@router.get("/config-sources", response_model=list[ClassConfigSourceRead])
def list_config_sources(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Classes the caller may copy a template from, archived ones included."""
    role = (user_service.role_name_of(user) or "").upper()
    query = db.query(Class)
    if role != "ADMIN":
        allowed = course_service.class_ids_for_trainer(db, user.id)
        if not allowed:
            return []
        query = query.filter(Class.id.in_(allowed))
    rows = query.order_by(Class.code).all()
    return [
        ClassConfigSourceRead(id=c.id, code=c.code, name=c.name, status=c.status) for c in rows
    ]


@router.get("/{class_id}/info", response_model=ClassInfoRead)
def read_info(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The class page: teachers/admins write it, enrolled students read it."""
    section = course_service.get_class(db, class_id)
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")
    role = (user_service.role_name_of(user) or "").upper()
    is_trainer = role == "TRAINER" and section.id in course_service.class_ids_for_trainer(
        db, user.id
    )
    # Students only see the class page of a live class they are enrolled in.
    if (
        role != "ADMIN"
        and not is_trainer
        and (
            section.status == course_service.STATUS_ARCHIVED
            or not course_service.is_active_member(db, section.id, user.id)
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only this class' trainers and members can read its information.",
        )
    return ClassInfoRead(**course_service.get_info(db, section.id))


@router.patch("/{class_id}/info", response_model=ClassInfoRead)
def update_info(
    class_id: uuid.UUID,
    payload: ClassInfoUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change only the fields sent — omit a field to keep it, send "" to clear it."""
    section = _require_class_teacher(db, user, class_id)
    try:
        values = course_service.update_info(
            db, section.id, payload.model_dump(exclude_unset=True)
        )
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return ClassInfoRead(**values)


@router.get("/{class_id}/config", response_model=ClassConfigRead)
def read_config(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    section = _require_class_teacher(db, user, class_id)
    return ClassConfigRead(**course_service.get_config(db, section.id))


@router.patch("/{class_id}/config", response_model=ClassConfigRead)
def update_config(
    class_id: uuid.UUID,
    payload: ClassConfigUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Change only the fields sent — omitted fields keep their current value.

    The roster capacity is an admin decision: a trainer of the class may edit
    the teaching fields, but any patch carrying ``capacity`` is rejected whole.
    """
    section = _require_class_teacher(db, user, class_id)
    changes = payload.model_dump(exclude_unset=True)
    role = (user_service.role_name_of(user) or "").upper()
    if "capacity" in changes and role != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an admin can change the roster capacity.",
        )
    try:
        values = course_service.update_config(db, section.id, changes)
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return ClassConfigRead(**values)


@router.post("/{class_id}/config/copy", response_model=ClassConfigRead)
def copy_config(
    class_id: uuid.UUID,
    payload: ClassConfigCopy,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Copy another class' teaching config onto this one (capacity excluded)."""
    section = _require_class_teacher(db, user, class_id)
    role = (user_service.role_name_of(user) or "").upper()
    allowed = None if role == "ADMIN" else course_service.class_ids_for_trainer(db, user.id)
    try:
        values = course_service.copy_config(
            db, section.id, payload.source_class_id, allowed_source_ids=allowed
        )
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return ClassConfigRead(**values)


def _template_document(row: document_service.Document) -> TemplateDocumentRead:
    return TemplateDocumentRead(
        id=row.id,
        file_name=row.file_name,
        file_type=row.file_type,
        size_bytes=row.size_bytes,
        version=row.version,
    )


@router.get("/{class_id}/template-documents", response_model=list[TemplateDocumentRead])
def list_template_documents(
    class_id: uuid.UUID,
    source_class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Approved documents available to carry over from a template class."""
    section = _require_class_teacher(db, user, class_id)
    source = _require_template_source(db, user, source_class_id, section.id)
    return [
        _template_document(row) for row in document_service.copyable_documents(db, source.id)
    ]


@router.post("/{class_id}/template-copy", response_model=ClassTemplateCopyRead)
def copy_template(
    class_id: uuid.UUID,
    payload: ClassTemplateCopy,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Copy selected config/info fields and approved documents once.

    The source class is never mutated, and cloned documents are already
    approved/indexed in the target class - no second review or re-embedding.
    Empty source fields never clear the target's existing information.
    """
    section = _require_class_teacher(db, user, class_id)
    source = _require_template_source(db, user, payload.source_class_id, section.id)
    unknown_fields = set(payload.info_fields) - set(course_service.INFO_FIELDS)
    if unknown_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown class information field: {', '.join(sorted(unknown_fields))}.",
        )
    role = (user_service.role_name_of(user) or "").upper()
    allowed = None if role == "ADMIN" else course_service.class_ids_for_trainer(db, user.id)
    try:
        config_values = (
            course_service.copy_config(
                db, section.id, source.id, allowed_source_ids=allowed
            )
            if payload.copy_config
            else course_service.get_config(db, section.id)
        )
        source_info = course_service.get_info(db, source.id)
        info_changes = {
            field: source_info[field]
            for field in payload.info_fields
            if source_info[field] is not None and str(source_info[field]).strip()
        }
        info_values = (
            course_service.update_info(db, section.id, info_changes)
            if info_changes
            else course_service.get_info(db, section.id)
        )
    except DomainError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    copied, skipped = document_service.clone_documents_to_class(
        db, source.id, section.id, payload.document_ids, user
    )
    return ClassTemplateCopyRead(
        config=ClassConfigRead(**config_values),
        info=ClassInfoRead(**info_values),
        copied_documents=[_template_document(row) for row in copied],
        skipped_documents=[
            TemplateDocumentSkip(file_name=name, reason=reason) for name, reason in skipped
        ],
    )
