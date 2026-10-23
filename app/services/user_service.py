"""Account management — admin-created only (AC-03, AC-11, AC-12).

ERD (AI-119): ``user`` holds profile + ``role_id`` (FK ``role``),
``account`` holds ``password_hash``. Role names are UPPERCASE in the DB
(STUDENT / TEACHING_ASSISTANT / TRAINER / ADMIN); the API accepts the
lowercase aliases from the .xlsx ``type`` column.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
import uuid

from sqlalchemy.orm import Session

from app.core.security import generate_password, hash_password
from app.models.course import Class, Enrollment
from app.models.user import Account, Role, User
from app.services import course_service as course_service_mod

ROLE_ADMIN = "ADMIN"
ROLE_TRAINER = "TRAINER"
ROLE_TA = "TEACHING_ASSISTANT"
ROLE_STUDENT = "STUDENT"
VALID_ROLES = (ROLE_ADMIN, ROLE_TRAINER, ROLE_TA, ROLE_STUDENT)
# Aliases accepted in the .xlsx `type` column (AC-05).
ROLE_ALIASES = {
    "student": ROLE_STUDENT,
    "trainer": ROLE_TRAINER,
    "teaching assistant": ROLE_TA,
    "teaching_assistant": ROLE_TA,
    "ta": ROLE_TA,
    "assistant": ROLE_TA,
    "admin": ROLE_ADMIN,
}
# Roles that must be assigned to a class before they can log in (AC-03).
ROLES_REQUIRING_CLASS = (ROLE_TRAINER, ROLE_TA)


class DomainError(ValueError):
    pass


STUDENT_CODE_RE = re.compile(r"^[A-Z]+_[0-9]{4}$")
STAFF_CODE_RE = STUDENT_CODE_RE


def _initials(full_name: str) -> str:
    """First letters of each name token, ASCII uppercased (Tran Van A -> TVA)."""
    ascii_name = "".join(
        c for c in unicodedata.normalize("NFD", full_name) if not unicodedata.combining(c)
    )
    letters = [t[0].upper() for t in ascii_name.split() if t and t[0].isalpha()]
    return "".join(letters) or "S"


def generate_student_code(db: Session, full_name: str) -> str:
    """Random MSSV as INITIALS_NNNN (e.g. TVA_4821).

    The 4-digit part is random, not sequential; collisions with existing
    codes of the same prefix are retried (unguessable, no ordering leak).
    """
    prefix = _initials(full_name)
    like = f"{prefix}_%"
    existing = [r[0] for r in db.query(User.student_code).filter(User.student_code.like(like)).all()]
    taken = set()
    for code in existing:
        m = re.fullmatch(rf"{re.escape(prefix)}_([0-9]{{4}})", code or "")
        if m:
            taken.add(m.group(1))
    # 10_000 possible suffixes; bail out rather than loop forever.
    if len(taken) >= 10_000:
        raise DomainError(f"Student code space exhausted for prefix '{prefix}'.")
    for _ in range(100):
        suffix = f"{secrets.randbelow(10_000):04d}"
        if suffix not in taken:
            return f"{prefix}_{suffix}"
    raise DomainError(f"Could not allocate a student code for prefix '{prefix}'.")


def generate_staff_code(db: Session, full_name: str) -> str:
    """Random MSNV as INITIALS_NNNN, mirroring MSSV generation."""
    prefix = _initials(full_name)
    like = f"{prefix}_%"
    existing = [r[0] for r in db.query(User.staff_code).filter(User.staff_code.like(like)).all()]
    taken = set()
    for code in existing:
        m = re.fullmatch(rf"{re.escape(prefix)}_([0-9]{{4}})", code or "")
        if m:
            taken.add(m.group(1))
    if len(taken) >= 10_000:
        raise DomainError(f"Staff code space exhausted for prefix '{prefix}'.")
    for _ in range(100):
        suffix = f"{secrets.randbelow(10_000):04d}"
        if suffix not in taken:
            return f"{prefix}_{suffix}"
    raise DomainError(f"Could not allocate a staff code for prefix '{prefix}'.")


def normalize_student_code(raw: str | None) -> str | None:
    """Validate an admin-supplied MSSV, or None when blank (auto-generate)."""
    if raw is None:
        return None
    code = raw.strip().upper()
    if not code:
        return None
    if not STUDENT_CODE_RE.fullmatch(code):
        raise DomainError(
            f"Invalid student code '{raw}'. Expected format INITIALS_NNNN (e.g. TVA_0001)."
        )
    return code


def normalize_staff_code(raw: str | None) -> str | None:
    """Normalize an admin-supplied MSNV, or None when blank."""
    if raw is None:
        return None
    code = raw.strip().upper()
    if not code:
        return None
    if len(code) > 20:
        raise DomainError("Staff code must be at most 20 characters.")
    return code


def normalize_role(raw: str) -> str:
    key = (raw or "").strip().lower().replace("_", " ").replace("-", " ")
    key = " ".join(key.split())
    if key in ROLE_ALIASES:
        return ROLE_ALIASES[key]
    raise DomainError(f"Invalid role '{raw}'. Expected student, trainer or teaching assistant.")


def role_name_of(user: User) -> str:
    """Return the UPPERCASE role name for a user (works with joined role)."""
    role = getattr(user, "role", None)
    if role is not None and getattr(role, "name", None):
        return role.name
    return ""


def resolve_class(db: Session, class_code: str | None) -> Class | None:
    if not class_code:
        return None
    code = class_code.strip()
    section = db.query(Class).filter(Class.code == code).first()
    if section is None:
        raise DomainError(f"Class '{code}' does not exist — create it first.")
    return section


def _coerce_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def create_user(
    db: Session,
    username: str,
    email: str,
    role_raw: str,
    class_code: str | None,
    password: str | None = None,
    student_code: str | None = None,
    staff_code: str | None = None,
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
    # MSSV: only students carry one; blank means auto-generate INITIALS_NNNN.
    code = normalize_student_code(student_code)
    if role == ROLE_STUDENT and code is None:
        code = generate_student_code(db, username)
    if code is not None:
        if role != ROLE_STUDENT:
            raise DomainError("Student code is only for student accounts.")
        if db.query(User).filter(User.student_code == code).first():
            raise DomainError(f"Student code '{code}' already exists.")
    staff = normalize_staff_code(staff_code)
    if role != ROLE_STUDENT and staff is None:
        staff = generate_staff_code(db, username)
    if staff is not None:
        if role == ROLE_STUDENT:
            raise DomainError("Staff code is only for staff accounts.")
        if db.query(User).filter(User.staff_code == staff).first():
            raise DomainError(f"Staff code '{staff}' already exists.")
    section = resolve_class(db, class_code)
    # AC-03: creation is allowed without a class so the first trainer can
    # exist before the first class (class.trainer_id is NOT NULL). The login
    # gate (auth_service) still blocks trainer/TA without a class.
    role_row = db.query(Role).filter(Role.name == role).first()
    if role_row is None:
        raise DomainError(f"Role '{role}' is not seeded — run migrations/seeds first.")
    plain = password or generate_password()
    user = User(
        full_name=username.strip(),
        email=email_norm,
        role_id=role_row.id,
        status="ACTIVE",
        student_code=code,
        staff_code=staff,
    )
    db.add(user)
    db.flush()
    db.add(Account(user_id=user.id, password_hash=hash_password(plain), is_active=True))
    db.flush()
    # Keep class <-> staff assignment consistent (AC-02: exactly one
    # trainer per class). Never silently steal an occupied slot — the admin
    # reassigns explicitly via PATCH /classes/{id}/assign.
    if section is not None:
        if role == ROLE_TRAINER:
            if section.trainer_id is not None:
                db.delete(user)
                db.commit()
                raise DomainError(
                    f"Class '{section.code}' already has a trainer — reassign it explicitly."
                )
            section.trainer_id = user.id
        else:
            # Students and TAs join through enrollment (ERD, no ta_id column).
            # Hard cap: 20 credits per (semester, academic_year). New users start
            # at 0 so this only bites if the flow ever enrolls existing users.
            try:
                course_service_mod.check_credit_cap(db, user.id, section)
            except ValueError:
                db.rollback()
                raise
            existing = (
                db.query(Enrollment)
                .filter(
                    Enrollment.class_id == section.id,
                    Enrollment.student_id == user.id,
                )
                .first()
            )
            if existing is None:
                db.add(
                    Enrollment(
                        class_id=section.id,
                        student_id=user.id,
                        status="ACTIVE",
                    )
                )
    db.commit()
    db.refresh(user)
    return user, plain


def list_users(
    db: Session, role: str | None = None, class_id: uuid.UUID | str | None = None
) -> list[User]:
    q = db.query(User).order_by(User.email)
    if role is not None:
        # Accept both "trainer" and "TRAINER".
        try:
            role_up = normalize_role(role)
        except DomainError:
            role_up = role.strip().upper()
        q = q.join(Role, User.role_id == Role.id).filter(Role.name == role_up)
    if class_id is not None:
        cid = _coerce_uuid(class_id)
        # Trainer of the class OR enrolled in it.
        enrolled_ids = [
            r[0] for r in db.query(Enrollment.student_id).filter(Enrollment.class_id == cid).all()
        ]
        trainer_ids = [r[0] for r in db.query(Class.trainer_id).filter(Class.id == cid).all()]
        wanted = set(enrolled_ids) | set(trainer_ids)
        if not wanted:
            return []
        q = q.filter(User.id.in_(wanted))
    return q.all()


def set_active(db: Session, user_id: uuid.UUID | str, is_active: bool) -> User:
    """AC-11: deactivate so the account can no longer log in."""
    uid = _coerce_uuid(user_id)
    user = db.query(User).filter(User.id == uid).first()
    if user is None:
        raise DomainError("Account does not exist.")
    user.status = "ACTIVE" if is_active else "INACTIVE"
    account = db.query(Account).filter(Account.user_id == uid).first()
    if account is not None:
        account.is_active = is_active
    db.commit()
    db.refresh(user)
    return user


def reset_password(db: Session, user_id: uuid.UUID | str) -> tuple[User, str]:
    """AC-11: generate a new password; caller emails it to the account."""
    uid = _coerce_uuid(user_id)
    user = db.query(User).filter(User.id == uid).first()
    if user is None:
        raise DomainError("Account does not exist.")
    account = db.query(Account).filter(Account.user_id == uid).first()
    if account is None:
        raise DomainError("Account credentials do not exist.")
    plain = generate_password()
    account.password_hash = hash_password(plain)
    account.is_active = True
    if user.status == "INACTIVE":
        user.status = "ACTIVE"
    db.commit()
    db.refresh(user)
    return user, plain


def delete_user(db: Session, user_id: uuid.UUID | str) -> None:
    """AC-12: confirmation happens in the UI; the last admin cannot be deleted."""
    uid = _coerce_uuid(user_id)
    user = db.query(User).filter(User.id == uid).first()
    if user is None:
        raise DomainError("Account does not exist.")
    rname = role_name_of(user)
    if not rname:
        row = db.query(Role).filter(Role.id == user.role_id).first()
        rname = row.name if row else ""
    if rname == ROLE_ADMIN:
        admin_count = (
            db.query(User).join(Role, User.role_id == Role.id).filter(Role.name == ROLE_ADMIN).count()
        )
        if admin_count <= 1:
            raise DomainError("The last admin account cannot be deleted.")
    # ERD: class.trainer_id is NOT NULL + RESTRICT — a trainer owning a
    # class cannot be deleted before the class is reassigned or removed.
    owned = db.query(Class).filter(Class.trainer_id == uid).first()
    if owned is not None:
        raise DomainError(
            f"Account owns class '{owned.code}' — reassign its trainer or delete the class first."
        )
    # Remove enrollments for this user.
    db.query(Enrollment).filter(Enrollment.student_id == uid).delete()
    db.delete(user)
    db.commit()
