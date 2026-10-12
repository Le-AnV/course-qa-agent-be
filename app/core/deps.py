"""FastAPI dependencies: DB session, current user, RBAC guards, bootstrap admin."""

from __future__ import annotations

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
    """Create the first admin so a fresh DB is usable (login works before any admin exists)."""
    from app.models.user import ROLE_ADMIN, User

    existing = db.query(User).filter(User.role == ROLE_ADMIN).first()
    if existing is not None:
        return
    admin = User(
        username="admin",
        email=settings.bootstrap_admin_email.lower(),
        password_hash=hash_password(settings.bootstrap_admin_password),
        role=ROLE_ADMIN,
        class_id=None,
        is_active=True,
    )
    db.add(admin)
    db.commit()


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
):
    from app.models.user import User

    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")
    payload = decode_access_token(credentials.credentials)
    if payload is None or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    try:
        user_id = int(payload["sub"])
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token.")
    user = db.query(User).filter(User.id == user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=LOGIN_GENERIC_ERROR)
    # Role change requires re-login: token role must match the DB role.
    if payload.get("role") != user.role:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Role changed, please log in again.")
    return user


def require_admin(current_user=Depends(get_current_user)):
    from app.models.user import ROLE_ADMIN

    if current_user.role != ROLE_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only.")
    return current_user


def require_class_member(current_user=Depends(get_current_user)):
    """AC-04 helper: every non-admin caller is scoped to their own class.

    Chat/RAG services must filter documents and history by
    ``current_user.class_id`` — never answer from another class's materials.
    """
    from app.models.user import ROLE_ADMIN

    if current_user.role != ROLE_ADMIN and current_user.class_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account is not assigned to a class.")
    return current_user
