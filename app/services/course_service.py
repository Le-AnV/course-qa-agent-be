"""Course + class management (AC-01, AC-02, AC-12)."""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.course import ClassSection, Course
from app.models.user import ROLE_STUDENT, ROLE_TA, ROLE_TRAINER, User


class DomainError(ValueError):
    pass


def create_course(db: Session, code: str, title: str) -> Course:
    code = code.strip()
    if db.query(Course).filter(Course.code == code).first():
        raise DomainError(f"Course code '{code}' already exists.")
    course = Course(code=code, title=title.strip())
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


def list_courses(db: Session) -> list[Course]:
    return db.query(Course).order_by(Course.code).all()


def get_course(db: Session, course_id: int) -> Course | None:
    return db.query(Course).filter(Course.id == course_id).first()


def create_class(db: Session, course_id: int, code: str) -> ClassSection:
    """AC-01: the course must exist first — a class is always created under it."""
    course = get_course(db, course_id)
    if course is None:
        raise DomainError("Course does not exist — create the course first.")
    code = code.strip()
    exists = (
        db.query(ClassSection)
        .filter(ClassSection.course_id == course_id, ClassSection.code == code)
        .first()
    )
    if exists:
        raise DomainError(f"Class '{code}' already exists in course '{course.code}'.")
    section = ClassSection(course_id=course_id, code=code)
    db.add(section)
    db.commit()
    db.refresh(section)
    return section


def list_classes(db: Session, course_id: int | None = None) -> list[ClassSection]:
    q = db.query(ClassSection)
    if course_id is not None:
        q = q.filter(ClassSection.course_id == course_id)
    return q.order_by(ClassSection.code).all()


def get_class(db: Session, class_id: int) -> ClassSection | None:
    return db.query(ClassSection).filter(ClassSection.id == class_id).first()


def assign_staff(
    db: Session, class_id: int, trainer_id: int | None, ta_id: int | None
) -> ClassSection:
    """AC-02: exactly one trainer per class; same trainer may teach several classes."""
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    if trainer_id is not None:
        trainer = db.query(User).filter(User.id == trainer_id).first()
        if trainer is None or trainer.role != ROLE_TRAINER:
            raise DomainError("Trainer must be an existing account with role 'trainer'.")
    if ta_id is not None:
        ta = db.query(User).filter(User.id == ta_id).first()
        if ta is None or ta.role != ROLE_TA:
            raise DomainError(
                "Teaching assistant must be an existing account with role 'teaching_assistant'."
            )
    section.trainer_id = trainer_id
    section.ta_id = ta_id
    # Link staff to the class they serve.
    for uid in (trainer_id, ta_id):
        if uid is not None:
            staff = db.query(User).filter(User.id == uid).first()
            if staff is not None:
                staff.class_id = class_id
    db.commit()
    db.refresh(section)
    return section


def class_warnings(db: Session, class_id: int) -> dict:
    """AC-12: deleting a class warns first if it still has documents or tickets."""
    # Optional tables (land with later epics) — count only if present.
    document_count = 0
    ticket_count = 0
    try:
        from app.models.document import Document  # type: ignore

        document_count = db.query(Document).filter(Document.class_id == class_id).count()
    except (ImportError, SQLAlchemyError):
        document_count = 0
    try:
        from app.models.ticket import Ticket  # type: ignore

        ticket_count = db.query(Ticket).filter(Ticket.class_id == class_id).count()
    except (ImportError, SQLAlchemyError):
        ticket_count = 0
    member_count = db.query(User).filter(User.class_id == class_id).count()
    return {
        "document_count": document_count,
        "ticket_count": ticket_count,
        "member_count": member_count,
    }


def delete_class(db: Session, class_id: int) -> dict:
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    warnings = class_warnings(db, class_id)
    # Unlink members (students keep accounts, lose class scope until reassigned).
    db.query(User).filter(User.class_id == class_id).update({User.class_id: None})
    db.delete(section)
    db.commit()
    return warnings


def count_students(db: Session, class_id: int) -> int:
    return db.query(User).filter(User.class_id == class_id, User.role == ROLE_STUDENT).count()


def user_display_name(db: Session, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = db.query(User).filter(User.id == user_id).first()
    return user.username if user else None
