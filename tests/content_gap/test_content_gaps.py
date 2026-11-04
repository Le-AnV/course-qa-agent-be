"""Phase 3 unit tests for Content Gap Analytics, Deduplication, and Reason Grouping (AI-91 AC-09 to AC-20)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.category import ClassCategory
from app.models.chat import ChatMessage, ChatSession
from app.models.course import Class, Enrollment
from app.models.user import Role, User
from app.services import content_gap_service


@pytest.fixture()
def db():
    """Isolated in-memory SQLite session for content gap tests."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = SessionLocal()

    role = Role(name="TRAINER", description="Trainer role")
    session.add(role)
    session.flush()

    trainer = User(
        id=uuid.uuid4(),
        role_id=role.id,
        email="trainer@test.edu.vn",
        full_name="Prof Test",
    )
    session.add(trainer)
    session.flush()

    test_class = Class(
        id=uuid.uuid4(),
        trainer_id=trainer.id,
        code="CS202",
        name="Object-Oriented Programming",
        semester="1",
        academic_year="2026-2027",
    )
    session.add(test_class)
    session.flush()

    # Add 10 students
    student_role = Role(name="STUDENT", description="Student role")
    session.add(student_role)
    session.flush()

    students = []
    for i in range(10):
        st = User(
            id=uuid.uuid4(),
            role_id=student_role.id,
            email=f"student{i}@test.edu.vn",
            full_name=f"Student {i}",
        )
        session.add(st)
        session.flush()
        students.append(st)

        en = Enrollment(
            class_id=test_class.id,
            student_id=st.id,
            status="ACTIVE",
        )
        session.add(en)

    session.commit()
    return session


def test_classify_question_reason():
    """Verify pedagogical classification logic."""
    rtype, title, _ = content_gap_service._classify_question_reason(
        "How to write C++ code example using std::unique_ptr with custom destructors?"
    )
    assert rtype == "missing_code"
    assert "Code Implementation" in title

    rtype, title, _ = content_gap_service._classify_question_reason(
        "What is the difference between shared_ptr vs unique_ptr in performance?"
    )
    assert rtype == "missing_comparison"
    assert "Comparison" in title

    rtype, title, _ = content_gap_service._classify_question_reason(
        "Why does my program throw a segmentation fault core dumped error on delete?"
    )
    assert rtype == "debugging_error"
    assert "Debugging" in title

    rtype, title, _ = content_gap_service._classify_question_reason(
        "Explain the theoretical definition of polymorphism in OOP."
    )
    assert rtype == "missing_concept"
    assert "Concept Not Covered" in title


def test_compute_content_gap_dashboard_returns_preview_when_empty(db):
    """When a class has no chat sessions, returns realistic illustrative preview."""
    test_class = db.query(Class).first()
    assert test_class is not None

    resp = content_gap_service.compute_content_gap_dashboard(db, test_class.id, force_refresh=True)

    assert resp.class_id == test_class.id
    assert len(resp.topics) > 0
    assert resp.total_students_enrolled == 10
    # Top topics must have reason_groups with questions
    crit_topic = next((t for t in resp.topics if t.status == "critical"), None)
    assert crit_topic is not None
    assert len(crit_topic.reason_groups) > 0
    assert any(rg.reason_type == "missing_code" for rg in crit_topic.reason_groups)
    assert len(crit_topic.reason_groups[0].questions) > 0


def test_deduplication_and_threshold_rules(db):
    """Verify max 1 count per student per category per calendar day (AC-09) and critical threshold (AC-10-14)."""
    test_class = db.query(Class).first()
    students = db.query(User).filter(User.email.like("student%")).all()
    assert len(students) >= 3

    # Create category
    cat = ClassCategory(
        class_id=test_class.id,
        name="Smart Pointers",
    )
    db.add(cat)
    db.commit()

    now = datetime.now(UTC)

    # Student 0 asks 5 questions on the SAME day about smart pointers with web fallback
    # Should deduplicate to 1 web count
    sess0 = ChatSession(user_id=students[0].id, class_id=test_class.id)
    db.add(sess0)
    db.commit()

    for i in range(5):
        u_msg = ChatMessage(
            session_id=sess0.id,
            role="USER",
            content=f"How to use Smart Pointers code example {i}?",
            created_at=now - timedelta(minutes=i * 5),
        )
        a_msg = ChatMessage(
            session_id=sess0.id,
            role="ASSISTANT",
            content="Answer",
            used_web_search=True,
            created_at=now - timedelta(minutes=i * 5),
        )
        db.add(u_msg)
        db.add(a_msg)

    # Student 1 asks on the SAME day -> should count as second distinct student
    sess1 = ChatSession(user_id=students[1].id, class_id=test_class.id)
    db.add(sess1)
    db.commit()
    db.add(ChatMessage(session_id=sess1.id, role="USER", content="Smart Pointers vs raw pointers?", created_at=now))
    db.add(ChatMessage(session_id=sess1.id, role="ASSISTANT", content="Ans", used_web_search=True, created_at=now))

    # Student 2 asks on a DIFFERENT day -> adds 1 more web search count (total 3 web fallbacks)
    sess2 = ChatSession(user_id=students[2].id, class_id=test_class.id)
    db.add(sess2)
    db.commit()
    yesterday = now - timedelta(days=1)
    db.add(ChatMessage(session_id=sess2.id, role="USER", content="Smart Pointers memory leak bug", created_at=yesterday))
    db.add(ChatMessage(session_id=sess2.id, role="ASSISTANT", content="Ans", used_web_search=True, created_at=yesterday))

    db.commit()

    resp = content_gap_service.compute_content_gap_dashboard(db, test_class.id, force_refresh=True)

    topic = next((t for t in resp.topics if t.category_id == str(cat.id)), None)
    assert topic is not None
    # 5 questions from student 0 + 1 from student 1 + 1 from student 2 = 7 total questions
    assert topic.web_fallbacks == 7
    # 3 distinct students on distinct days -> 3 deduplicated web searches (AC-09)
    assert topic.dedup_web_fallbacks == 3
    assert topic.distinct_students == 3
    assert topic.status == "critical"  # >= 3 searches, >= 2 students (30% of class >= 10%), ratio >= 30%

    # Check reason groups
    assert len(topic.reason_groups) >= 2
    r_types = [rg.reason_type for rg in topic.reason_groups]
    assert "missing_code" in r_types or "missing_comparison" in r_types


def test_clear_dashboard_cache():
    """Verify cache invalidation."""
    cid = uuid.uuid4()
    content_gap_service._DASHBOARD_CACHE[cid] = (datetime.now(UTC), MagicMock())
    assert cid in content_gap_service._DASHBOARD_CACHE

    content_gap_service.clear_dashboard_cache(cid)
    assert cid not in content_gap_service._DASHBOARD_CACHE
