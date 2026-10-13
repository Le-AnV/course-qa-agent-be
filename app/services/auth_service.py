"""Login + profile logic (E1: AI-25 login rules, AI-27 RBAC re-checks).

ERD (AI-119): ``user`` holds profile + ``role_id``, ``account`` holds
``password_hash``. Role names are UPPERCASE in the DB; the JWT carries the
lowercase alias for FE compat.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.security import LOGIN_GENERIC_ERROR, create_access_token, verify_password
from app.models.course import Class, Enrollment
from app.models.user import Account, Role, User

# Roles that must be assigned to a class before they can log in (AC-03).
ROLES_REQUIRING_CLASS = ("TRAINER", "TEACHING_ASSISTANT")


def _role_name(db: Session, user: User) -> str:
    role = getattr(user, "role", None)
    if role is not None and getattr(role, "name", None):
        return role.name
    row = db.query(Role).filter(Role.id == user.role_id).first()
    return row.name if row else ""


def authenticate(db: Session, email: str, password: str) -> User | None:
    """Return the user on success, None on any failure.

    AI-25: login errors must not reveal whether an email exists — wrong email
    and wrong password produce the same generic error at the router layer.
    """
    user = db.query(User).filter(User.email == email.strip().lower()).first()
    if user is None or user.status != "ACTIVE":
        return None
    account = db.query(Account).filter(Account.user_id == user.id).first()
    if account is None or not account.is_active:
        return None
    if not verify_password(password, account.password_hash):
        return None
    # AC-03: trainer/TA without a class cannot log in yet.
    rname = _role_name(db, user)
    if rname in ROLES_REQUIRING_CLASS:
        if rname == "TRAINER":
            owns = db.query(Class).filter(Class.trainer_id == user.id).first()
            if owns is None:
                return None
        else:
            enrolled = (
                db.query(Enrollment)
                .filter(
                    Enrollment.student_id == user.id,
                    Enrollment.status == "ACTIVE",
                )
                .first()
            )
            if enrolled is None:
                return None
    return user


def issue_token(user: User, db: Session | None = None) -> str:
    rname = getattr(getattr(user, "role", None), "name", None)
    if not rname and db is not None:
        rname = _role_name(db, user)
    return create_access_token(str(user.id), (rname or "student").lower())


def login_error() -> str:
    # Single message for: unknown email, wrong password, inactive account,
    # or role awaiting class assignment.
    return LOGIN_GENERIC_ERROR
