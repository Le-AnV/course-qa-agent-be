"""Account management — admin-created only (AC-03, AC-11, AC-12)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.security import generate_password, hash_password
from app.models.course import ClassSection
from app.models.user import (
    ROLE_ADMIN,
    ROLE_ALIASES,
    ROLE_TA,
    ROLE_TRAINER,
    ROLES_REQUIRING_CLASS,
    VALID_ROLES,
    User,
)


class DomainError(ValueError):
    pass


def normalize_role(raw: str) -> str:
    key = (raw or "").strip().lower().replace("_", " ").replace("-", " ")
    key = " ".join(key.split())
    if key in ROLE_ALIASES:
        return ROLE_ALIASES[key]
    raise DomainError(f"Invalid role '{raw}'. Expected student, trainer or teaching assistant.")


def resolve_class(db: Session, class_code: str | None) -> ClassSection | None:
    if not class_code:
        return None
    code = class_code.strip()
    section = db.query(ClassSection).filter(ClassSection.code == code).first()
    if section is None:
        raise DomainError(f"Class '{code}' does not exist — create it first.")
    return section


def create_user(
    db: Session,
    username: str,
    email: str,
    role_raw: str,
    class_code: str | None,
    password: str | None = None,
) -> tuple[User, str]:
    """Create one account. Returns (user, plaintext password) — the caller
    emails the password immediately and never persists or returns it."""
    role = normalize_role(role_raw)
    if role not in VALID_ROLES:
        raise DomainError(f"Invalid role '{role_raw}'.")
    if role == ROLE_ADMIN:
        raise DomainError("Admin accounts are created separately, not via this endpoint.")
    email_norm = email.strip().lower()
    if db.query(User).filter(User.email == email_norm).first():
        raise DomainError(f"Email '{email_norm}' already exists — skipped.")
    section = resolve_class(db, class_code)
    # AC-03: trainer/TA must be assigned to a class before they can log in.
    if role in ROLES_REQUIRING_CLASS and section is None:
        raise DomainError(f"Role '{role}' requires a class assignment before login.")
    plain = password or generate_password()
    user = User(
        username=username.strip(),
        email=email_norm,
        password_hash=hash_password(plain),
        role=role,
        class_id=section.id if section else None,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    # Keep class <-> trainer/TA assignment consistent (AC-02: exactly one
    # trainer per class). Never silently steal an occupied slot — the admin
    # reassigns explicitly via PATCH /classes/{id}/assign.
    if user.class_id is not None and role in (ROLE_TRAINER, ROLE_TA):
        section = db.query(ClassSection).filter(ClassSection.id == user.class_id).first()
        if section is not None:
            slot = section.trainer_id if role == ROLE_TRAINER else section.ta_id
            if slot is not None:
                db.delete(user)
                db.commit()
                label = "trainer" if role == ROLE_TRAINER else "teaching assistant"
                raise DomainError(
                    f"Class '{section.code}' already has a {label} — reassign it explicitly."
                )
            if role == ROLE_TRAINER:
                section.trainer_id = user.id
            else:
                section.ta_id = user.id
            db.commit()
    return user, plain


def list_users(
    db: Session, role: str | None = None, class_id: int | None = None
) -> list[User]:
    q = db.query(User).order_by(User.id)
    if role is not None:
        q = q.filter(User.role == role)
    if class_id is not None:
        q = q.filter(User.class_id == class_id)
    return q.all()


def set_active(db: Session, user_id: int, is_active: bool) -> User:
    """AC-11: deactivate so the account can no longer log in."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise DomainError("Account does not exist.")
    user.is_active = is_active
    db.commit()
    db.refresh(user)
    return user


def reset_password(db: Session, user_id: int) -> tuple[User, str]:
    """AC-11: generate a new password; caller emails it to the account."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise DomainError("Account does not exist.")
    plain = generate_password()
    user.password_hash = hash_password(plain)
    db.commit()
    db.refresh(user)
    return user, plain


def delete_user(db: Session, user_id: int) -> None:
    """AC-12: confirmation happens in the UI; the last admin cannot be deleted."""
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise DomainError("Account does not exist.")
    if user.role == ROLE_ADMIN:
        admin_count = db.query(User).filter(User.role == ROLE_ADMIN).count()
        if admin_count <= 1:
            raise DomainError("The last admin account cannot be deleted.")
    # Unlink class staff slots pointing at this user.
    db.query(ClassSection).filter(ClassSection.trainer_id == user_id).update(
        {ClassSection.trainer_id: None}
    )
    db.query(ClassSection).filter(ClassSection.ta_id == user_id).update(
        {ClassSection.ta_id: None}
    )
    db.delete(user)
    db.commit()
