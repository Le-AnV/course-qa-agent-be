"""Login + profile logic (E1: AI-25 login rules, AI-27 RBAC re-checks)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.security import LOGIN_GENERIC_ERROR, create_access_token, verify_password
from app.models.user import ROLES_REQUIRING_CLASS, User


def authenticate(db: Session, email: str, password: str) -> User | None:
    """Return the user on success, None on any failure.

    AI-25: login errors must not reveal whether an email exists — wrong email
    and wrong password produce the same generic error at the router layer.
    """
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    # AC-03: trainer/TA without a class cannot log in yet.
    if user.role in ROLES_REQUIRING_CLASS and user.class_id is None:
        return None
    return user


def issue_token(user: User) -> str:
    return create_access_token(str(user.id), user.role)


def login_error() -> str:
    # Single message for: unknown email, wrong password, inactive account,
    # or role awaiting class assignment.
    return LOGIN_GENERIC_ERROR
