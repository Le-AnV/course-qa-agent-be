"""Login + profile logic (E1: AI-25 login rules, AI-27 RBAC re-checks).

ERD (AI-119): ``user`` holds profile + ``role_id``, ``account`` holds
``password_hash``. Role names are UPPERCASE in the DB; the JWT carries the
lowercase alias for FE compat.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.security import (
    LOGIN_GENERIC_ERROR,
    create_access_token,
    hash_password,
    verify_password,
)
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


class ProfileError(ValueError):
    """Domain error for profile updates — carries the HTTP status for the router."""

    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.status_code = status_code


def update_profile(db: Session, user: User, full_name: str) -> User:
    """AC-03: rename the caller's own display name (user.full_name).

    Only the name is editable here — email/role/class are admin-managed
    (AI-31). The FE re-reads /me afterwards so the rename applies immediately.
    """
    name = (full_name or "").strip()
    if not name:
        raise ProfileError("Display name must not be empty.", 422)
    if len(name) > 100:
        raise ProfileError("Display name must be at most 100 characters.", 422)
    user.full_name = name
    db.commit()
    db.refresh(user)
    return user


def change_password(db: Session, user: User, old_password: str, new_password: str) -> None:
    """AC-02: verify the old password, then replace the bcrypt hash.

    Wrong old password returns the generic login error — never reveal whether
    it was "close". New password rules: min 8 chars, must differ from old.
    """
    if new_password == old_password:
        raise ProfileError("New password must differ from the old password.", 422)
    account = db.query(Account).filter(Account.user_id == user.id).first()
    if account is None or not account.is_active:
        raise ProfileError(login_error(), 404)
    if not verify_password(old_password, account.password_hash):
        raise ProfileError(login_error(), 401)
    account.password_hash = hash_password(new_password)
    db.commit()
