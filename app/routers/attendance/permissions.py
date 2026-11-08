"""Authorization and role checking helpers for attendance routers (AI-136)."""

from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.course import Class
from app.models.user import User
from app.services import course_service, user_service


def require_class_staff(db: Session, user: User, class_id: uuid.UUID) -> Class:
    """Ensure the caller is an admin or a primary/co-trainer of the class.

    Returns:
        The Class entity if authorized.

    Raises:
        HTTPException(404): If the class does not exist.
        HTTPException(403): If the caller lacks permission.
    """
    clazz = course_service.get_class(db, class_id)
    if clazz is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Class not found.",
        )
    role = (user_service.role_name_of(user) or "").upper()
    if role == "ADMIN":
        return clazz
    if role == "TRAINER" and clazz.id in course_service.class_ids_for_trainer(
        db, user.id, active_only=True
    ):
        return clazz
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only this class' teachers can manage attendance.",
    )


def require_class_member_or_staff(db: Session, user: User, class_id: uuid.UUID) -> Class:
    """Ensure the caller is staff or an active enrolled student of the class.

    Returns:
        The Class entity if authorized.

    Raises:
        HTTPException(404): If the class does not exist.
        HTTPException(403): If the caller is neither staff nor an enrolled member.
    """
    clazz = course_service.get_class(db, class_id)
    if clazz is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Class not found.",
        )
    role = (user_service.role_name_of(user) or "").upper()
    if role == "ADMIN":
        return clazz
    if role == "TRAINER" and clazz.id in course_service.class_ids_for_trainer(
        db, user.id, active_only=True
    ):
        return clazz
    if course_service.is_active_member(db, class_id, user.id):
        return clazz
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You do not have permission to view sessions for this class.",
    )


def require_enrolled_student(db: Session, user: User, class_id: uuid.UUID) -> Class:
    """Ensure the caller has an active enrollment in this class.

    Returns:
        The Class entity if authorized.

    Raises:
        HTTPException(404): If the class does not exist.
        HTTPException(403): If the caller is not actively enrolled.
    """
    clazz = course_service.get_class(db, class_id)
    if clazz is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Class not found.",
        )
    if not course_service.is_active_member(db, class_id, user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not enrolled in this class.",
        )
    return clazz

