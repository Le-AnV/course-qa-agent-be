"""FastAPI dependencies: DB session, current user, RBAC guards, bootstrap admin.

ERD (AI-119): ``user`` holds profile + ``role_id``, ``account`` holds
``password_hash``. Role names are UPPERCASE (ADMIN / TRAINER / STUDENT /
TEACHING_ASSISTANT); JWT carries the lowercase alias for FE compat.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.core.database import get_session_local
from app.core.security import LOGIN_GENERIC_ERROR, decode_access_token, hash_password

bearer_scheme = HTTPBearer(auto_error=False)


def get_db() -> Generator[Session, None, None]:
    SessionLocal = get_session_local(settings.database_url)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_bootstrap_admin(db: Session) -> None:
    """Create the first admin so a fresh DB is usable (login works before any admin exists).

    Also seeds the static role rows (ERD, AI-119) — account creation and the
    login class-gate both require them.
    """
    from app.models.user import Account, Role, User

    for name, description in (
        ("STUDENT", "Student account provisioned by an administrator or bulk import."),
        ("TEACHING_ASSISTANT", "Teaching assistant; permissions must be assigned explicitly."),
        ("TRAINER", "Trainer responsible for assigned classes and their learning materials."),
        ("ADMIN", "Administrator responsible for platform management."),
    ):
        if db.query(Role).filter(Role.name == name).first() is None:
            db.add(Role(name=name, description=description))
    db.flush()
    role = db.query(Role).filter(Role.name == "ADMIN").first()
    existing = (
        db.query(User).join(Role, User.role_id == Role.id).filter(Role.name == "ADMIN").first()
    )
    if existing is not None:
        return
    admin = User(
        full_name="System Administrator",
        email=settings.bootstrap_admin_email.lower(),
        role_id=role.id,
        status="ACTIVE",
    )
    db.add(admin)
    db.flush()
    db.add(
        Account(
            user_id=admin.id,
            password_hash=hash_password(settings.bootstrap_admin_password),
            is_active=True,
        )
    )
    db.commit()


def _role_name(db: Session, user) -> str:
    role = getattr(user, "role", None)
    if role is not None and getattr(role, "name", None):
        return role.name
    row = db.query(_role_table()).filter(_role_table().id == user.role_id).first()
    return row.name if row else ""


def _role_table():
    from app.models.user import Role

    return Role


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
):
    from app.models.user import Account, User

    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    payload = decode_access_token(credentials.credentials)
    if payload is None or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    try:
        user_id = uuid.UUID(str(payload["sub"]))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or user.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=LOGIN_GENERIC_ERROR)
    account = db.query(Account).filter(Account.user_id == user.id).first()
    if account is not None and not account.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=LOGIN_GENERIC_ERROR)
    # Role change requires re-login: token role must match the DB role
    # (compare case-insensitively: token carries lowercase alias).
    if payload.get("role", "").upper() != _role_name(db, user).upper():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Role changed, please log in again.")
    return user


def require_admin(current_user=Depends(get_current_user)):
    from app.core.database import get_session_local as _gsl

    # get_current_user already loaded the user; resolve role via a fresh lookup
    # only when the relationship is not populated.
    role_name = getattr(getattr(current_user, "role", None), "name", None)
    if role_name is None:
        SessionLocal = _gsl(settings.database_url)
        db = SessionLocal()
        try:
            role_name = _role_name(db, current_user)
        finally:
            db.close()
    if role_name != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only.")
    return current_user


def require_class_member(current_user=Depends(get_current_user)):
    """AC-04 helper: every non-admin caller is scoped to their own class.

    ERD: trainers own classes (``class.trainer_id``), students/TAs join
    through ``enrollment``. Chat/RAG services must filter by that scope —
    never answer from another class's materials.
    """
    from app.core.database import get_session_local as _gsl
    from app.models.course import Class, Enrollment

    SessionLocal = _gsl(settings.database_url)
    db = SessionLocal()
    try:
        role_name = _role_name(db, current_user)
        if role_name == "ADMIN":
            return current_user
        if role_name == "TRAINER":
            owns = db.query(Class).filter(Class.trainer_id == current_user.id).first()
            if owns is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account is not assigned to a class.",
                )
            return current_user
        enrolled = (
            db.query(Enrollment)
            .filter(
                Enrollment.student_id == current_user.id,
                Enrollment.status == "ACTIVE",
            )
            .first()
        )
        if enrolled is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is not assigned to a class.",
            )
        return current_user
    finally:
        db.close()
