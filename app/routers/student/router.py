"""Student workspace endpoints — enrolled classes, members, materials.

Prefix: /api/student. Every endpoint is membership-scoped: a student/TA
only sees classes they are enrolled in (ACTIVE), a trainer only classes
they own, admins everything. No mock data — 404/empty when nothing exists.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.course import Class, Course, Enrollment
from app.models.document import Document
from app.models.user import User

router = APIRouter(prefix="/api/student", tags=["student"])


class StudentClassRead(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    course_code: str
    course_title: str
    trainer_name: str | None
    semester: str
    academic_year: str
    member_count: int
    document_count: int

    model_config = {"from_attributes": True}


class ClassMemberRead(BaseModel):
    id: uuid.UUID
    username: str
    email: str
    role: str

    model_config = {"from_attributes": True}


class ClassDocumentRead(BaseModel):
    id: uuid.UUID
    file_name: str
    file_type: str
    size_bytes: int
    status: str
    created_at: str

    model_config = {"from_attributes": True}


class CatalogClassRead(StudentClassRead):
    """One class in the catalog plus the caller's relation to it."""

    my_status: str = "none"  # active | pending | none


class JoinRequestRead(BaseModel):
    id: uuid.UUID
    class_id: uuid.UUID
    class_code: str
    student_id: uuid.UUID
    username: str
    email: str
    role: str
    status: str

    model_config = {"from_attributes": True}


def _role_name(db: Session, user: User) -> str:
    role = getattr(user, "role", None)
    if role is not None and getattr(role, "name", None):
        return role.name
    from app.models.user import Role

    row = db.query(Role).filter(Role.id == user.role_id).first()
    return row.name if row else ""


def _trainer_name(db: Session, trainer_id: uuid.UUID) -> str | None:
    trainer = db.query(User).filter(User.id == trainer_id).first()
    return trainer.full_name if trainer else None


def _course_of(db: Session, course_id: uuid.UUID) -> Course | None:
    return db.query(Course).filter(Course.id == course_id).first()


def _member_class_ids(db: Session, user: User) -> set[uuid.UUID] | None:
    """Class ids visible to the caller, or None for admins (everything)."""
    role = _role_name(db, user).upper()
    if role == "ADMIN":
        return None
    if role == "TRAINER":
        return {c.id for c in db.query(Class.id).filter(Class.trainer_id == user.id).all()}
    rows = (
        db.query(Enrollment.class_id)
        .filter(Enrollment.student_id == user.id, Enrollment.status == "ACTIVE")
        .all()
    )
    return {r[0] for r in rows}


def _require_member(db: Session, user: User, class_id: uuid.UUID) -> Class:
    section = db.query(Class).filter(Class.id == class_id).first()
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")
    allowed = _member_class_ids(db, user)
    if allowed is not None and section.id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not a member of this class.",
        )
    return section


def _coerce_uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")


def _to_read(db: Session, section: Class) -> StudentClassRead:
    course = _course_of(db, section.course_id)
    members = (
        db.query(Enrollment).filter(Enrollment.class_id == section.id, Enrollment.status == "ACTIVE").count()
    )
    docs = db.query(Document).filter(Document.class_id == section.id).count()
    return StudentClassRead(
        id=section.id,
        code=section.code,
        name=section.name,
        course_code=course.code if course else "",
        course_title=course.name if course else "",
        trainer_name=_trainer_name(db, section.trainer_id),
        semester=section.semester,
        academic_year=section.academic_year,
        member_count=members + 1,  # + trainer
        document_count=docs,
    )


@router.get("/classes", response_model=list[StudentClassRead])
def my_classes(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Classes the caller belongs to — enrollments for students/TAs, owned classes for trainers."""
    allowed = _member_class_ids(db, user)
    q = db.query(Class).order_by(Class.code)
    if allowed is not None:
        if not allowed:
            return []
        q = q.filter(Class.id.in_(allowed))
    return [_to_read(db, s) for s in q.all()]


@router.get("/classes/{class_id}", response_model=StudentClassRead)
def class_detail(
    class_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """One class header for the workspace banner/overview (membership-checked)."""
    return _to_read(db, _require_member(db, user, _coerce_uuid(class_id)))


@router.get("/classes/{class_id}/members", response_model=list[ClassMemberRead])
def class_members(
    class_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Trainer + enrolled students of one class (membership-checked)."""
    from app.services import user_service

    section = _require_member(db, user, _coerce_uuid(class_id))
    out: list[ClassMemberRead] = []
    trainer = db.query(User).filter(User.id == section.trainer_id).first()
    if trainer is not None:
        out.append(
            ClassMemberRead(
                id=trainer.id,
                username=trainer.full_name,
                email=trainer.email,
                role="trainer",
            )
        )
    students = (
        db.query(User)
        .join(Enrollment, Enrollment.student_id == User.id)
        .filter(Enrollment.class_id == section.id, Enrollment.status == "ACTIVE")
        .order_by(User.full_name)
        .all()
    )
    for s in students:
        role = (user_service.role_name_of(s) or "STUDENT").lower()
        out.append(ClassMemberRead(id=s.id, username=s.full_name, email=s.email, role=role))
    return out


@router.get("/classes/{class_id}/documents", response_model=list[ClassDocumentRead])
def class_documents(
    class_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Approved (indexed) materials of one class for its members (syllabus tab)."""
    section = _require_member(db, user, _coerce_uuid(class_id))
    rows = (
        db.query(Document)
        .filter(Document.class_id == section.id, Document.status == "indexed")
        .order_by(Document.created_at.desc())
        .all()
    )
    return [
        ClassDocumentRead(
            id=r.id,
            file_name=r.file_name,
            file_type=r.file_type,
            size_bytes=r.size_bytes,
            status=r.status,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


# --- Enrollment requests: students browse + request, TAs approve ---

REQUESTABLE_ROLES = ("STUDENT", "TEACHING_ASSISTANT")


def _enrollment_of(db: Session, user_id: uuid.UUID, class_id: uuid.UUID) -> Enrollment | None:
    return (
        db.query(Enrollment)
        .filter(Enrollment.class_id == class_id, Enrollment.student_id == user_id)
        .first()
    )


def _can_approve(db: Session, user: User, section: Class) -> bool:
    """Admins, the class trainer, or a TA enrolled in the class may approve."""
    role = _role_name(db, user).upper()
    if role == "ADMIN":
        return True
    if section.trainer_id == user.id:
        return True
    if role == "TEACHING_ASSISTANT":
        mine = _enrollment_of(db, user.id, section.id)
        return mine is not None and mine.status == "ACTIVE"
    return False


def _to_request_read(db: Session, enr: Enrollment) -> JoinRequestRead:
    from app.services import user_service

    section = db.query(Class).filter(Class.id == enr.class_id).first()
    student = db.query(User).filter(User.id == enr.student_id).first()
    return JoinRequestRead(
        id=enr.id,
        class_id=enr.class_id,
        class_code=section.code if section else "",
        student_id=enr.student_id,
        username=student.full_name if student else "",
        email=student.email if student else "",
        role=(user_service.role_name_of(student) or "STUDENT").lower() if student else "student",
        status=enr.status.lower(),
    )


@router.get("/catalog", response_model=list[CatalogClassRead])
def catalog(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Every class with the caller's relation to it (browse + register)."""
    sections = db.query(Class).order_by(Class.code).all()
    out: list[CatalogClassRead] = []
    for section in sections:
        enr = _enrollment_of(db, user.id, section.id)
        if enr is not None and enr.status == "ACTIVE":
            my_status = "active"
        elif enr is not None and enr.status == "PENDING":
            my_status = "pending"
        elif section.trainer_id == user.id:
            my_status = "active"
        else:
            my_status = "none"
        base = _to_read(db, section)
        out.append(CatalogClassRead(**base.model_dump(), my_status=my_status))
    return out


@router.post("/classes/{class_id}/request", response_model=JoinRequestRead)
def request_join(
    class_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Request to join a class — a TA (or the trainer) approves later."""
    if _role_name(db, user).upper() not in REQUESTABLE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only students and teaching assistants can request to join a class.",
        )
    section = db.query(Class).filter(Class.id == _coerce_uuid(class_id)).first()
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")
    existing = _enrollment_of(db, user.id, section.id)
    if existing is not None and existing.status == "ACTIVE":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You are already a member of this class.")
    if existing is not None and existing.status == "PENDING":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Your request is already pending approval.")
    enr = Enrollment(class_id=section.id, student_id=user.id, status="PENDING")
    db.add(enr)
    db.commit()
    db.refresh(enr)
    return _to_request_read(db, enr)


@router.get("/requests", response_model=list[JoinRequestRead])
def my_requests(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The caller's own pending join requests."""
    rows = (
        db.query(Enrollment)
        .filter(Enrollment.student_id == user.id, Enrollment.status == "PENDING")
        .all()
    )
    return [_to_request_read(db, e) for e in rows]


@router.delete("/classes/{class_id}/request")
def cancel_request(
    class_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Withdraw your own pending join request."""
    section = db.query(Class).filter(Class.id == _coerce_uuid(class_id)).first()
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")
    enr = _enrollment_of(db, user.id, section.id)
    if enr is None or enr.status != "PENDING":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No pending request found.")
    db.delete(enr)
    db.commit()
    return {"cancelled": str(section.id)}


@router.get("/classes/{class_id}/requests", response_model=list[JoinRequestRead])
def pending_requests(
    class_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Pending join requests for one class (TA / trainer / admin review queue)."""
    section = db.query(Class).filter(Class.id == _coerce_uuid(class_id)).first()
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")
    if not _can_approve(db, user, section):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the class TA, trainer or an admin can review requests.")
    rows = (
        db.query(Enrollment)
        .filter(Enrollment.class_id == section.id, Enrollment.status == "PENDING")
        .all()
    )
    return [_to_request_read(db, e) for e in rows]


@router.post("/classes/{class_id}/requests/{student_id}/approve", response_model=JoinRequestRead)
def approve_request(
    class_id: str,
    student_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Approve one join request — the student becomes an ACTIVE member."""
    section = db.query(Class).filter(Class.id == _coerce_uuid(class_id)).first()
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")
    if not _can_approve(db, user, section):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the class TA, trainer or an admin can approve requests.")
    try:
        sid = uuid.UUID(str(student_id))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    enr = _enrollment_of(db, sid, section.id)
    if enr is None or enr.status != "PENDING":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    enr.status = "ACTIVE"
    db.commit()
    db.refresh(enr)
    return _to_request_read(db, enr)


@router.post("/classes/{class_id}/requests/{student_id}/reject")
def reject_request(
    class_id: str,
    student_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Reject one join request — the row is removed so the student may retry later."""
    section = db.query(Class).filter(Class.id == _coerce_uuid(class_id)).first()
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")
    if not _can_approve(db, user, section):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the class TA, trainer or an admin can reject requests.")
    try:
        sid = uuid.UUID(str(student_id))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    enr = _enrollment_of(db, sid, section.id)
    if enr is None or enr.status != "PENDING":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Request not found.")
    db.delete(enr)
    db.commit()
    return {"rejected": str(sid), "class_id": str(section.id)}
