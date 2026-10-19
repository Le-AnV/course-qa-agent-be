"""Class configuration — capacity (P5) and typed teaching config (P6).

Prefix: /api/classes. Writable by admins and by the trainers of that class
(primary or co-trainer); students never see this surface. Each class has one
configuration row, and any class' configuration can be copied onto another as
a reusable template.
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
)
from app.services import course_service, user_service
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


@router.get("/config-sources", response_model=list[ClassConfigSourceRead])
def list_config_sources(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Classes the caller may copy a configuration from (never its own)."""
    role = (user_service.role_name_of(user) or "").upper()
    query = db.query(Class).filter(Class.status != course_service.STATUS_ARCHIVED)
    if role != "ADMIN":
        allowed = course_service.class_ids_for_trainer(db, user.id)
        if not allowed:
            return []
        query = query.filter(Class.id.in_(allowed))
    rows = query.order_by(Class.code).all()
    return [ClassConfigSourceRead(id=c.id, code=c.code, name=c.name) for c in rows]


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
    if role != "ADMIN" and not is_trainer:
        # Students only see the class page of a live class they are enrolled in.
        if section.status == course_service.STATUS_ARCHIVED or not course_service.is_active_member(
            db, section.id, user.id
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
    """Change only the fields sent — omitted fields keep their current value."""
    section = _require_class_teacher(db, user, class_id)
    changes = payload.model_dump(exclude_unset=True)
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
