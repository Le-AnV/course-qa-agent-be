"""Tests for the Phase 2a backfill (plan v3): existing rows become course-shared v1.

Uses in-memory SQLite (StaticPool) so CI stays DB-free.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core import backfill_versions
from app.core.backfill_versions import normalize_name
from app.core.database import Base, get_session_local, reset_engine
from app.core.seed import seed_database
from app.models.course import Class, Course
from app.models.document import Document, DocumentEvent, DocumentVersion
from app.models.user import User

TEST_DB_URL = "sqlite://"


@pytest.fixture()
def db_session(monkeypatch):
    import app.core.database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", TEST_DB_URL)

    engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    dbmod._db_url = TEST_DB_URL
    Base.metadata.create_all(bind=engine)
    SessionLocal = get_session_local(TEST_DB_URL)
    db: Session = SessionLocal()
    try:
        seed_database(db)
        course = Course(code="PY101", name="Python", description="d", status="ACTIVE")
        db.add(course)
        db.flush()
        teacher = db.query(User).filter(User.email == "teacher@academic.edu.vn").first()
        for code in ("PY101-A", "PY101-B"):
            db.add(
                Class(
                    course_id=course.id,
                    trainer_id=teacher.id,
                    code=code,
                    name=code,
                    semester="HK1",
                    academic_year="2026-2027",
                    status="ACTIVE",
                )
            )
        db.commit()
        yield db
    finally:
        db.close()
        engine.dispose()
    reset_engine()


def _teacher(db: Session) -> User:
    return db.query(User).filter(User.email == "teacher@academic.edu.vn").first()


def _class(db: Session, code: str) -> Class:
    return db.query(Class).filter(Class.code == code).first()


def _legacy_document(
    db: Session,
    file_name: str,
    class_id,
    teacher_id,
    status: str = "pending",
    review_note: str | None = None,
) -> Document:
    """A pre-2a row: no course_id/scope/name_normalized/version yet."""

    doc = Document(
        class_id=class_id,
        uploaded_by=teacher_id,
        file_name=file_name,
        file_type=".pdf",
        mime_type="application/pdf",
        size_bytes=12,
        status=status,
        review_note=review_note,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


def test_normalize_name_nfc_case_trim():
    assert normalize_name("  Bài1.PDF ") == normalize_name("bài1.pdf")
    assert normalize_name("A") == "a"


def test_apply_backfills_course_v1_with_note(db_session):
    teacher = _teacher(db_session)
    clazz = _class(db_session, "PY101-A")
    record = _legacy_document(
        db_session,
        "notes.pdf",
        clazz.id,
        teacher.id,
        status="rejected",
        review_note="Old edition.",
    )

    report = backfill_versions.apply(db_session)

    assert report["to_backfill"] == 1
    db_session.refresh(record)
    assert record.created_by == teacher.id
    assert record.course_id == clazz.course_id
    assert record.name_normalized == "notes.pdf"
    assert record.needs_review is False
    assert record.current_version_id is not None
    version = (
        db_session.query(DocumentVersion).filter(DocumentVersion.document_id == record.id).one()
    )
    assert (version.version_no, version.status) == (1, "rejected")
    assert version.review_note == "Old edition."
    assert version.uploaded_by == teacher.id
    assert record.current_version_id == version.id
    event = db_session.query(DocumentEvent).filter(DocumentEvent.document_id == record.id).one()
    assert (event.action, event.version_no) == ("backfill", 1)


def test_clash_flagged_never_merged(db_session):
    teacher = _teacher(db_session)
    _legacy_document(db_session, "Lab.PDF", _class(db_session, "PY101-A").id, teacher.id)
    _legacy_document(db_session, "lab.pdf", _class(db_session, "PY101-B").id, teacher.id)

    report = backfill_versions.apply(db_session)

    assert len(report["clash_groups"]) == 1
    rows = db_session.query(Document).all()
    assert all(r.needs_review for r in rows)
    # Two separate groups, not merged.
    assert db_session.query(DocumentVersion).count() == 2


def test_same_name_different_courses_not_flagged(db_session):
    teacher = _teacher(db_session)
    course = Course(code="PY102", name="Python 2", description="d", status="ACTIVE")
    db_session.add(course)
    db_session.flush()
    db_session.add(
        Class(
            course_id=course.id,
            trainer_id=teacher.id,
            code="PY102-A",
            name="PY102-A",
            semester="HK1",
            academic_year="2026-2027",
            status="ACTIVE",
        )
    )
    db_session.commit()
    _legacy_document(db_session, "lab.pdf", _class(db_session, "PY101-A").id, teacher.id)
    _legacy_document(db_session, "lab.pdf", _class(db_session, "PY102-A").id, teacher.id)

    report = backfill_versions.apply(db_session)

    assert report["clash_groups"] == []
    assert all(not r.needs_review for r in db_session.query(Document).all())


def test_dry_run_writes_nothing_and_apply_idempotent(db_session):
    teacher = _teacher(db_session)
    _legacy_document(db_session, "a.pdf", _class(db_session, "PY101-A").id, teacher.id)

    dry = backfill_versions.dry_run(db_session)
    assert dry["to_backfill"] == 1
    assert db_session.query(DocumentVersion).count() == 0

    first = backfill_versions.apply(db_session)
    second = backfill_versions.apply(db_session)
    assert first["to_backfill"] == 1
    assert second["to_backfill"] == 0
    assert db_session.query(DocumentVersion).count() == 1
