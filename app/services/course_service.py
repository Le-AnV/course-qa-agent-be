"""Course + class management (AC-01, AC-02, AC-12).

ERD (AI-119): ``course`` / ``class`` / ``enrollment`` with UUID PKs.
``class.trainer_id`` is NOT NULL — a class is always created with a trainer.
Students/TAs join through ``enrollment`` (no ``ta_id`` column, no
``User.class_id``). Login gates (trainer must own a class, student must be
enrolled) are enforced at login time, not at creation time, so the onboarding
order is: create trainer account -> create class with that trainer.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.course import Class, Course, Enrollment
from app.models.user import Role, User


class DomainError(ValueError):
    pass


# Credit rules: each class carries manual credits (1-6); a student is hard-capped
# at 20 credits per (semester, academic_year) across ACTIVE enrollments.
MIN_CLASS_CREDITS = 1
MAX_CLASS_CREDITS = 6
MAX_SEMESTER_CREDITS = 20


def _coerce_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def create_course(db: Session, code: str, title: str) -> Course:
    code = code.strip()
    if db.query(Course).filter(Course.code == code).first():
        raise DomainError(f"Course code '{code}' already exists.")
    course = Course(code=code, name=title.strip())
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


def list_courses(db: Session) -> list[Course]:
    return db.query(Course).order_by(Course.code).all()


def get_course(db: Session, course_id: uuid.UUID | str) -> Course | None:
    return db.query(Course).filter(Course.id == _coerce_uuid(course_id)).first()


def create_class(
    db: Session,
    course_id: uuid.UUID | str,
    code: str,
    trainer_id: uuid.UUID | str | None = None,
    name: str | None = None,
    semester: str = "S1",
    academic_year: str = "2026",
    credits: int = 3,
) -> Class:
    """AC-01: the course must exist first — a class is always created under it.

    ERD requires ``class.trainer_id`` NOT NULL, so a trainer must be supplied.
    """
    cid = _coerce_uuid(course_id)
    course = get_course(db, cid)
    if course is None:
        raise DomainError("Course does not exist — create the course first.")
    code = code.strip()
    exists = (
        db.query(Class)
        .filter(Class.course_id == cid, Class.code == code)
        .first()
    )
    if exists:
        raise DomainError(f"Class '{code}' already exists in course '{course.code}'.")
    if trainer_id is None:
        raise DomainError("A trainer is required — create the trainer account first.")
    try:
        credits = int(credits)
    except (TypeError, ValueError):
        raise DomainError("Credits must be an integer between 1 and 6.")
    if not MIN_CLASS_CREDITS <= credits <= MAX_CLASS_CREDITS:
        raise DomainError(
            f"Credits must be between {MIN_CLASS_CREDITS} and {MAX_CLASS_CREDITS}."
        )
    tid = _coerce_uuid(trainer_id)
    trainer = db.query(User).filter(User.id == tid).first()
    if trainer is None:
        raise DomainError("Trainer account does not exist.")
    section = Class(
        course_id=cid,
        trainer_id=tid,
        code=code,
        name=(name or code).strip(),
        semester=semester.strip(),
        academic_year=academic_year.strip(),
        credits=credits,
    )
    db.add(section)
    db.commit()
    db.refresh(section)
    return section


def list_classes(db: Session, course_id: uuid.UUID | str | None = None) -> list[Class]:
    q = db.query(Class)
    if course_id is not None:
        q = q.filter(Class.course_id == _coerce_uuid(course_id))
    return q.order_by(Class.code).all()


def get_class(db: Session, class_id: uuid.UUID | str) -> Class | None:
    return db.query(Class).filter(Class.id == _coerce_uuid(class_id)).first()


def _require_trainer(db: Session, trainer_id: uuid.UUID) -> User:
    trainer = db.query(User).filter(User.id == trainer_id).first()
    if trainer is None:
        raise DomainError("Trainer must be an existing account.")
    role = db.query(Role).filter(Role.id == trainer.role_id).first()
    if role is None or role.name not in ("TRAINER", "ADMIN"):
        raise DomainError("Trainer must be an existing account with role 'trainer'.")
    return trainer


def assign_staff(
    db: Session,
    class_id: uuid.UUID | str,
    trainer_id: uuid.UUID | str | None,
) -> Class:
    """AC-02: exactly one trainer per class; same trainer may teach several classes."""
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    if trainer_id is not None:
        _require_trainer(db, _coerce_uuid(trainer_id))
        section.trainer_id = _coerce_uuid(trainer_id)
    db.commit()
    db.refresh(section)
    return section


def class_warnings(db: Session, class_id: uuid.UUID | str) -> dict:
    """AC-12: deleting a class warns first if it still has documents or tickets."""
    cid = _coerce_uuid(class_id)
    # Optional tables (land with later epics) — count only if present.
    document_count = 0
    ticket_count = 0
    try:
        from app.models.document import Document  # type: ignore

        document_count = db.query(Document).filter(Document.class_id == cid).count()
    except (ImportError, SQLAlchemyError):
        document_count = 0
    try:
        from app.models.ticket import Ticket  # type: ignore

        ticket_count = db.query(Ticket).filter(Ticket.class_id == cid).count()
    except (ImportError, SQLAlchemyError):
        ticket_count = 0
    member_count = db.query(Enrollment).filter(Enrollment.class_id == cid).count()
    return {
        "document_count": document_count,
        "ticket_count": ticket_count,
        "member_count": member_count,
    }


def delete_class(db: Session, class_id: uuid.UUID | str) -> dict:
    cid = _coerce_uuid(class_id)
    section = get_class(db, cid)
    if section is None:
        raise DomainError("Class does not exist.")
    warnings = class_warnings(db, cid)
    # Remove enrollments first (RESTRICT on class FK).
    db.query(Enrollment).filter(Enrollment.class_id == cid).delete()
    db.delete(section)
    db.commit()
    return warnings


def count_students(db: Session, class_id: uuid.UUID | str) -> int:
    cid = _coerce_uuid(class_id)
    return (
        db.query(Enrollment)
        .join(User, Enrollment.student_id == User.id)
        .join(Role, User.role_id == Role.id)
        .filter(Enrollment.class_id == cid, Role.name == "STUDENT")
        .count()
    )


def user_display_name(db: Session, user_id: uuid.UUID | str | None) -> str | None:
    if user_id is None:
        return None
    user = db.query(User).filter(User.id == _coerce_uuid(user_id)).first()
    return user.full_name if user else None


def semester_credit_total(
    db: Session,
    student_id: uuid.UUID | str,
    semester: str,
    academic_year: str,
    exclude_class_id: uuid.UUID | str | None = None,
) -> int:
    """Sum ACTIVE enrollment credits for one student in one term."""
    sid = _coerce_uuid(student_id)
    q = (
        db.query(Class.credits)
        .join(Enrollment, Enrollment.class_id == Class.id)
        .filter(
            Enrollment.student_id == sid,
            Enrollment.status == "ACTIVE",
            Class.semester == semester,
            Class.academic_year == academic_year,
        )
    )
    if exclude_class_id is not None:
        q = q.filter(Class.id != _coerce_uuid(exclude_class_id))
    return sum(r[0] or 0 for r in q.all())


def check_credit_cap(db: Session, student_id: uuid.UUID | str, section: Class) -> None:
    """Hard cap: activating this enrollment must not exceed 20 credits/term."""
    total = semester_credit_total(
        db, student_id, section.semester, section.academic_year,
        exclude_class_id=section.id,
    )
    if total + (section.credits or 0) > MAX_SEMESTER_CREDITS:
        raise DomainError(
            f"Exceeds {MAX_SEMESTER_CREDITS} credits per semester "
            f"(currently {total}, class is {section.credits})."
        )
