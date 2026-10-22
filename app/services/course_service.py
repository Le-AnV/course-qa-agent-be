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

from app.models.course import (
    Class,
    ClassConfig,
    ClassInfo,
    ClassPrerequisite,
    ClassTrainer,
    Course,
    Enrollment,
)
from app.models.user import Role, User


class DomainError(ValueError):
    pass


# Credit rules: each class carries manual credits (1-6); a student is hard-capped
# at 20 credits per (semester, academic_year) across ACTIVE enrollments.
MIN_CLASS_CREDITS = 1
MAX_CLASS_CREDITS = 6
MAX_SEMESTER_CREDITS = 20

# Class lifecycle: classes are ARCHIVED (soft, hidden from teachers/students)
# instead of hard-deleted; only an archived class can be permanently deleted.
STATUS_ACTIVE = "ACTIVE"
STATUS_ARCHIVED = "ARCHIVED"
CLASS_STATUSES = (STATUS_ACTIVE, STATUS_ARCHIVED)

# Typed class configuration (P6). Constrained vocabularies — no free text.
DELIVERY_MODES = ("on_campus", "online", "hybrid")
LANGUAGES = ("en", "vi")
ANSWER_SCOPES = ("syllabus_only", "syllabus_first", "open")
MIN_CAPACITY = 1
MAX_CAPACITY = 500
MIN_ANSWER_WORDS = 50
MAX_ANSWER_WORDS = 1000
MIN_SESSION_MINUTES = 30
MAX_SESSION_MINUTES = 180

# Defaults mirror the ClassConfig column defaults so an unsaved class still
# reports a complete configuration.
CONFIG_DEFAULTS: dict = {
    "capacity": None,
    "delivery_mode": "on_campus",
    "default_language": "en",
    "answer_scope": "syllabus_first",
    "citation_required": True,
    "max_answer_words": 250,
    "session_length_minutes": 90,
}
# Capacity is per-class (it depends on each roster), so copying a template
# deliberately leaves it out.
COPIABLE_CONFIG_FIELDS = (
    "delivery_mode",
    "default_language",
    "answer_scope",
    "citation_required",
    "max_answer_words",
    "session_length_minutes",
)


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
    code: str,
    trainer_id: uuid.UUID | str | None = None,
    name: str | None = None,
    semester: str = "S1",
    academic_year: str = "2026",
    credits: int = 3,
    prerequisite_class_ids: list[uuid.UUID] | None = None,
) -> Class:
    """Create a standalone class (the class IS the subject — no course binding).

    ERD requires ``class.trainer_id`` NOT NULL, so a trainer must be supplied.
    """
    code = code.strip()
    if db.query(Class).filter(Class.code == code).first():
        raise DomainError(f"Class '{code}' already exists.")
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
    prereq_ids = _validate_prerequisite_ids(db, prerequisite_class_ids, class_id=None)
    section = Class(
        trainer_id=tid,
        code=code,
        name=(name or code).strip(),
        semester=semester.strip(),
        academic_year=academic_year.strip(),
        credits=credits,
    )
    db.add(section)
    db.flush()
    for pid in prereq_ids:
        db.add(ClassPrerequisite(class_id=section.id, prerequisite_class_id=pid))
    db.commit()
    db.refresh(section)
    return section


def list_classes(db: Session) -> list[Class]:
    return db.query(Class).order_by(Class.code).all()


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


def list_trainers(db: Session, class_id: uuid.UUID | str) -> list[dict]:
    """All trainers on a class: the primary first, then extra trainers."""
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    out: list[dict] = []
    seen: set[uuid.UUID] = set()
    primary = db.query(User).filter(User.id == section.trainer_id).first()
    if primary is not None:
        seen.add(primary.id)
        out.append(
            {
                "trainer_id": primary.id,
                "name": primary.full_name,
                "email": primary.email,
                "is_primary": True,
            }
        )
    rows = (
        db.query(ClassTrainer, User)
        .join(User, ClassTrainer.trainer_id == User.id)
        .filter(ClassTrainer.class_id == section.id)
        .order_by(ClassTrainer.created_at)
        .all()
    )
    for link, trainer in rows:
        if trainer.id in seen:
            continue
        seen.add(trainer.id)
        out.append(
            {
                "trainer_id": trainer.id,
                "name": trainer.full_name,
                "email": trainer.email,
                "is_primary": False,
            }
        )
    return out


def class_ids_for_trainer(
    db: Session, user_id: uuid.UUID | str, active_only: bool = False
) -> set[uuid.UUID]:
    """Classes a trainer can work on: primary slot plus co-trainer links.

    ``active_only`` drops archived classes, which are hidden from the workspace.
    """
    tid = _coerce_uuid(user_id)
    primary = db.query(Class.id).filter(Class.trainer_id == tid)
    co = (
        db.query(ClassTrainer.class_id)
        .join(Class, ClassTrainer.class_id == Class.id)
        .filter(ClassTrainer.trainer_id == tid)
    )
    if active_only:
        primary = primary.filter(Class.status != STATUS_ARCHIVED)
        co = co.filter(Class.status != STATUS_ARCHIVED)
    ids = {r[0] for r in primary.all()}
    ids.update(r[0] for r in co.all())
    return ids


def add_trainer(db: Session, class_id: uuid.UUID | str, trainer_id: uuid.UUID | str) -> dict:
    """Add a co-trainer so a second teacher can take over mid-semester."""
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    tid = _coerce_uuid(trainer_id)
    _require_trainer(db, tid)
    if tid == section.trainer_id:
        raise DomainError("That trainer is already the primary trainer of this class.")
    existing = (
        db.query(ClassTrainer)
        .filter(ClassTrainer.class_id == section.id, ClassTrainer.trainer_id == tid)
        .first()
    )
    if existing is not None:
        raise DomainError("That trainer is already assigned to this class.")
    db.add(ClassTrainer(class_id=section.id, trainer_id=tid))
    db.commit()
    return {"added": str(tid)}


def remove_trainer(db: Session, class_id: uuid.UUID | str, trainer_id: uuid.UUID | str) -> dict:
    """Remove a co-trainer. The primary trainer is changed via /assign."""
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    tid = _coerce_uuid(trainer_id)
    if tid == section.trainer_id:
        raise DomainError(
            "That is the primary trainer — assign a different primary trainer first."
        )
    removed = (
        db.query(ClassTrainer)
        .filter(ClassTrainer.class_id == section.id, ClassTrainer.trainer_id == tid)
        .delete()
    )
    if not removed:
        raise DomainError("That trainer is not assigned to this class.")
    db.commit()
    return {"removed": str(tid)}


def active_student_count(db: Session, class_id: uuid.UUID | str) -> int:
    """ACTIVE students on the roster (TAs are not counted against capacity)."""
    cid = _coerce_uuid(class_id)
    return (
        db.query(Enrollment)
        .join(User, Enrollment.student_id == User.id)
        .join(Role, User.role_id == Role.id)
        .filter(
            Enrollment.class_id == cid,
            Enrollment.status == "ACTIVE",
            Role.name == "STUDENT",
        )
        .count()
    )


def check_capacity(db: Session, class_id: uuid.UUID | str) -> None:
    """Block a new ACTIVE member when the class has reached its capacity."""
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    capacity = get_config(db, section.id)["capacity"]
    if capacity is None:
        return
    current = active_student_count(db, section.id)
    if current >= capacity:
        raise DomainError(
            f"Class '{section.code}' is full: {current}/{capacity} students. "
            "Ask an admin to raise the capacity (Admin > Classes)."
        )


# --- Class configuration (P5 capacity, P6 typed teaching config) ---


def _config_row(db: Session, class_id: uuid.UUID) -> ClassConfig | None:
    return db.query(ClassConfig).filter(ClassConfig.class_id == class_id).first()


def get_config(db: Session, class_id: uuid.UUID | str) -> dict:
    """Effective configuration for a class (defaults when nothing is saved)."""
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    values = {"class_id": section.id, **CONFIG_DEFAULTS}
    row = _config_row(db, section.id)
    if row is not None:
        values.update({field: getattr(row, field) for field in CONFIG_DEFAULTS})
    return values


def _validate_config_change(db: Session, section: Class, field: str, value: object) -> object:
    if field == "capacity":
        if value is None:
            return None
        try:
            capacity = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise DomainError("Capacity must be a whole number.")
        if not MIN_CAPACITY <= capacity <= MAX_CAPACITY:
            raise DomainError(f"Capacity must be between {MIN_CAPACITY} and {MAX_CAPACITY}.")
        current = active_student_count(db, section.id)
        if capacity < current:
            raise DomainError(
                f"Capacity cannot be lower than the {current} student(s) already enrolled."
            )
        return capacity
    if field == "delivery_mode":
        if value not in DELIVERY_MODES:
            raise DomainError(f"Delivery mode must be one of: {', '.join(DELIVERY_MODES)}.")
        return value
    if field == "default_language":
        if value not in LANGUAGES:
            raise DomainError(f"Language must be one of: {', '.join(LANGUAGES)}.")
        return value
    if field == "answer_scope":
        if value not in ANSWER_SCOPES:
            raise DomainError(f"Answer scope must be one of: {', '.join(ANSWER_SCOPES)}.")
        return value
    if field == "citation_required":
        if not isinstance(value, bool):
            raise DomainError("Citation requirement must be true or false.")
        return value
    if field == "max_answer_words":
        try:
            words = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise DomainError("Answer limit must be a whole number.")
        if not MIN_ANSWER_WORDS <= words <= MAX_ANSWER_WORDS:
            raise DomainError(
                f"Answer limit must be between {MIN_ANSWER_WORDS} and {MAX_ANSWER_WORDS} words."
            )
        return words
    if field == "session_length_minutes":
        try:
            minutes = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise DomainError("Session length must be a whole number.")
        if not MIN_SESSION_MINUTES <= minutes <= MAX_SESSION_MINUTES:
            raise DomainError(
                f"Session length must be between {MIN_SESSION_MINUTES} and "
                f"{MAX_SESSION_MINUTES} minutes."
            )
        return minutes
    raise DomainError(f"Unknown configuration field '{field}'.")


def update_config(db: Session, class_id: uuid.UUID | str, changes: dict) -> dict:
    """Upsert the changed fields only; untouched fields keep their values."""
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    clean = {
        field: _validate_config_change(db, section, field, value)
        for field, value in changes.items()
        if field in CONFIG_DEFAULTS
    }
    if not clean:
        return get_config(db, section.id)
    row = _config_row(db, section.id)
    if row is None:
        row = ClassConfig(class_id=section.id, **{**CONFIG_DEFAULTS, **clean})
        db.add(row)
    else:
        for field, value in clean.items():
            setattr(row, field, value)
    db.commit()
    return get_config(db, section.id)


def copy_config(
    db: Session,
    class_id: uuid.UUID | str,
    source_class_id: uuid.UUID | str,
    allowed_source_ids: set[uuid.UUID] | None = None,
) -> dict:
    """Copy the teaching config from another class (template reuse).

    Capacity is intentionally not copied - it depends on each class' own roster.
    ``allowed_source_ids`` limits which classes may be read: pass the caller's
    own classes for a trainer, ``None`` for an admin who may read every class.
    """
    target = get_class(db, class_id)
    if target is None:
        raise DomainError("Class does not exist.")
    source = get_class(db, source_class_id)
    if source is None:
        raise DomainError("The class you are copying from does not exist.")
    if source.id == target.id:
        raise DomainError("Pick a different class to copy the configuration from.")
    if source.status == STATUS_ARCHIVED:
        raise DomainError("That class is archived.")
    if allowed_source_ids is not None and source.id not in allowed_source_ids:
        raise DomainError("You can only copy the configuration from a class you teach.")
    source_values = get_config(db, source.id)
    return update_config(
        db, target.id, {field: source_values[field] for field in COPIABLE_CONFIG_FIELDS}
    )


# --- Class information (teacher-authored class page: description, links, notes) ---

INFO_FIELDS = ("description", "meeting_url", "group_url", "schedule_note", "note")
INFO_FIELD_LABELS = {
    "description": "Course description",
    "meeting_url": "Online class link",
    "group_url": "Class group link",
    "schedule_note": "Schedule",
    "note": "Additional information",
}
INFO_TEXT_LIMITS = {"description": 2000, "note": 2000}
INFO_SHORT_LIMITS = {"schedule_note": 200, "meeting_url": 500, "group_url": 500}
INFO_URL_FIELDS = ("meeting_url", "group_url")


def _info_row(db: Session, class_id: uuid.UUID) -> ClassInfo | None:
    return db.query(ClassInfo).filter(ClassInfo.class_id == class_id).first()


def get_info(db: Session, class_id: uuid.UUID | str) -> dict:
    """Class page values for a class (all empty when nothing is saved yet)."""
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    values: dict = {"class_id": section.id, **{field: None for field in INFO_FIELDS}}
    row = _info_row(db, section.id)
    if row is not None:
        values.update({field: getattr(row, field) for field in INFO_FIELDS})
    return values


def _validate_info_change(field: str, value: object) -> str | None:
    if field not in INFO_FIELDS:
        raise DomainError(f"Unknown class information field '{field}'.")
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None  # an empty input clears the value
    label = INFO_FIELD_LABELS[field]
    limit = INFO_TEXT_LIMITS.get(field) or INFO_SHORT_LIMITS[field]
    if len(text) > limit:
        raise DomainError(f"{label} must be {limit} characters or fewer.")
    if field in INFO_URL_FIELDS and not text.lower().startswith(("http://", "https://")):
        raise DomainError(f"{label} must start with http:// or https://.")
    return text


def update_info(db: Session, class_id: uuid.UUID | str, changes: dict) -> dict:
    """Upsert the class page; omitted fields keep their value, "" clears one."""
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    clean = {
        field: _validate_info_change(field, value)
        for field, value in changes.items()
        if field in INFO_FIELDS
    }
    if not clean:
        return get_info(db, section.id)
    row = _info_row(db, section.id)
    if row is None:
        row = ClassInfo(class_id=section.id)
        db.add(row)
    for field, value in clean.items():
        setattr(row, field, value)
    db.commit()
    return get_info(db, section.id)


def is_active_member(db: Session, class_id: uuid.UUID | str, user_id: uuid.UUID | str) -> bool:
    """True when the user has an ACTIVE enrollment in the class (student/TA)."""
    return (
        db.query(Enrollment)
        .filter(
            Enrollment.class_id == _coerce_uuid(class_id),
            Enrollment.student_id == _coerce_uuid(user_id),
            Enrollment.status == "ACTIVE",
        )
        .first()
        is not None
    )


def set_class_status(db: Session, class_id: uuid.UUID | str, new_status: str) -> Class:
    """Archive/restore a class. Archived classes are hidden from the workspace."""
    if new_status not in CLASS_STATUSES:
        raise DomainError(f"Status must be one of: {', '.join(CLASS_STATUSES)}.")
    section = get_class(db, class_id)
    if section is None:
        raise DomainError("Class does not exist.")
    section.status = new_status
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


def _validate_prerequisite_ids(
    db: Session,
    ids: list[uuid.UUID] | None,
    class_id: uuid.UUID | None,
) -> list[uuid.UUID]:
    """Dedupe; reject self-reference and unknown classes."""
    out: list[uuid.UUID] = []
    for raw in ids or []:
        pid = _coerce_uuid(raw)
        if class_id is not None and pid == class_id:
            raise DomainError("A class cannot be its own prerequisite.")
        if pid not in out:
            out.append(pid)
    if not out:
        return []
    found = {row[0] for row in db.query(Class.id).filter(Class.id.in_(out)).all()}
    unknown = [str(pid) for pid in out if pid not in found]
    if unknown:
        raise DomainError(f"Prerequisite class not found: {', '.join(unknown)}.")
    return out


def class_prerequisites(db: Session, class_id: uuid.UUID | str) -> list[Class]:
    cid = _coerce_uuid(class_id)
    return (
        db.query(Class)
        .join(ClassPrerequisite, ClassPrerequisite.prerequisite_class_id == Class.id)
        .filter(ClassPrerequisite.class_id == cid)
        .order_by(Class.code)
        .all()
    )


def set_class_prerequisites(
    db: Session,
    class_id: uuid.UUID | str,
    prerequisite_class_ids: list[uuid.UUID] | None,
) -> list[Class]:
    cid = _coerce_uuid(class_id)
    if get_class(db, cid) is None:
        raise DomainError("Class does not exist.")
    ids = _validate_prerequisite_ids(db, prerequisite_class_ids, cid)
    db.query(ClassPrerequisite).filter(ClassPrerequisite.class_id == cid).delete()
    for pid in ids:
        db.add(ClassPrerequisite(class_id=cid, prerequisite_class_id=pid))
    db.commit()
    return class_prerequisites(db, cid)


def missing_prerequisites(
    db: Session, student_id: uuid.UUID | str, section: Class
) -> list[Class]:
    """Prerequisites of ``section`` the student has not passed yet."""
    prereqs = class_prerequisites(db, section.id)
    if not prereqs:
        return []
    passed = {
        row[0]
        for row in db.query(Enrollment.class_id)
        .filter(
            Enrollment.student_id == _coerce_uuid(student_id),
            Enrollment.result == "PASSED",
            Enrollment.class_id.in_([p.id for p in prereqs]),
        )
        .all()
    }
    return [p for p in prereqs if p.id not in passed]


def check_prerequisites(
    db: Session, student_id: uuid.UUID | str, section: Class
) -> None:
    missing = missing_prerequisites(db, student_id, section)
    if missing:
        names = ", ".join(f"{m.code} — {m.name}" for m in missing)
        raise DomainError(f"Prerequisites not passed: {names}.")


def set_enrollment_result(
    db: Session,
    class_id: uuid.UUID | str,
    student_id: uuid.UUID | str,
    result: str | None,
) -> Enrollment:
    """Trainer marks a term result; PASSED unlocks dependent classes."""
    if result not in (None, "PASSED", "FAILED"):
        raise DomainError("Result must be PASSED, FAILED or null.")
    enr = (
        db.query(Enrollment)
        .filter(
            Enrollment.class_id == _coerce_uuid(class_id),
            Enrollment.student_id == _coerce_uuid(student_id),
        )
        .first()
    )
    if enr is None:
        raise DomainError("Student is not enrolled in this class.")
    enr.result = result
    db.commit()
    db.refresh(enr)
    return enr


def user_display_name(db: Session, user_id: uuid.UUID | str | None) -> str | None:
    if user_id is None:
        return None
    user = db.query(User).filter(User.id == _coerce_uuid(user_id)).first()
    return user.full_name if user else None
