"""Tests for Quiz SQLAlchemy models, constraints, and migration (AI-148)."""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.core.database import Base
from app.models.course import Class
from app.models.quiz import (
    Quiz,
    QuizAnswer,
    QuizAttempt,
    QuizOption,
    QuizQuestion,
)
from app.models.user import Role, User


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def _create_user(db, email: str = "instructor@example.com") -> User:
    role = db.query(Role).filter_by(name="TEACHER").first()
    if not role:
        role = Role(id=uuid.uuid4(), name="TEACHER")
        db.add(role)
        db.flush()
    user = User(
        id=uuid.uuid4(),
        email=email,
        full_name="Test Instructor",
        role_id=role.id,
        status="ACTIVE",
    )
    db.add(user)
    db.flush()
    return user


def _create_class(db, trainer_id: uuid.UUID | None = None, code: str = "CS101") -> Class:
    if trainer_id is None:
        trainer = _create_user(db, email=f"trainer_{uuid.uuid4().hex[:6]}@example.com")
        trainer_id = trainer.id

    cls = Class(
        id=uuid.uuid4(),
        trainer_id=trainer_id,
        code=code,
        name="Intro to Programming",
        credits=3,
        semester="Fall",
        academic_year="2026",
        status="ACTIVE",
    )
    db.add(cls)
    db.flush()
    return cls


def test_create_quiz_full_hierarchy(db_session):
    user = _create_user(db_session)
    cls = _create_class(db_session)

    quiz = Quiz(
        id=uuid.uuid4(),
        class_id=cls.id,
        creator_id=user.id,
        title="Midterm Examination",
        description="Comprehensive evaluation covering units 1-4",
        time_limit_minutes=45,
        passing_score=7.0,
        status="PUBLISHED",
    )
    db_session.add(quiz)
    db_session.flush()

    q1 = QuizQuestion(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        question_type="SINGLE_CHOICE",
        question="What is the time complexity of binary search?",
        points=1.0,
        difficulty="REMEMBER",
        order_index=0,
        explanation="Binary search divides the search space in half at each step.",
        source_reference="Algorithms Chapter 2",
    )
    db_session.add(q1)
    db_session.flush()

    opt_a = QuizOption(
        id=uuid.uuid4(),
        question_id=q1.id,
        option_label="A",
        option_text="O(log n)",
        is_correct=True,
        order_index=0,
    )
    opt_b = QuizOption(
        id=uuid.uuid4(),
        question_id=q1.id,
        option_label="B",
        option_text="O(n)",
        is_correct=False,
        order_index=1,
    )
    db_session.add_all([opt_a, opt_b])
    db_session.flush()

    # Attempt and answer
    student = _create_user(db_session, email="student@example.com")
    attempt = QuizAttempt(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        student_id=student.id,
        status="SUBMITTED",
        total_score=1.0,
        is_passed=True,
    )
    db_session.add(attempt)
    db_session.flush()

    ans = QuizAnswer(
        id=uuid.uuid4(),
        attempt_id=attempt.id,
        question_id=q1.id,
        selected_option_labels=["A"],
        score_awarded=1.0,
        breakdown={"correct": ["A"], "score": 1.0},
    )
    db_session.add(ans)
    db_session.commit()

    # Query back and verify relations
    loaded_quiz = db_session.get(Quiz, quiz.id)
    assert loaded_quiz is not None
    assert loaded_quiz.title == "Midterm Examination"
    assert len(loaded_quiz.questions) == 1
    assert loaded_quiz.questions[0].difficulty == "REMEMBER"
    assert len(loaded_quiz.questions[0].options) == 2
    assert loaded_quiz.questions[0].options[0].option_label == "A"
    assert loaded_quiz.questions[0].options[0].is_correct is True

    loaded_attempt = db_session.get(QuizAttempt, attempt.id)
    assert loaded_attempt is not None
    assert len(loaded_attempt.answers) == 1
    assert loaded_attempt.answers[0].selected_option_labels == ["A"]


def test_cascade_delete_quiz(db_session):
    user = _create_user(db_session)
    cls = _create_class(db_session)

    quiz = Quiz(
        id=uuid.uuid4(),
        class_id=cls.id,
        creator_id=user.id,
        title="Quiz to Delete",
        status="DRAFT",
    )
    db_session.add(quiz)
    db_session.flush()

    q = QuizQuestion(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        question_type="FILL_IN_BLANK",
        question="What port does DNS use by default?",
        points=1.0,
        difficulty="REMEMBER",
    )
    db_session.add(q)
    db_session.flush()

    attempt = QuizAttempt(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        student_id=user.id,
        status="IN_PROGRESS",
    )
    db_session.add(attempt)
    db_session.flush()

    ans = QuizAnswer(
        id=uuid.uuid4(),
        attempt_id=attempt.id,
        question_id=q.id,
        text_answer="53",
    )
    db_session.add(ans)
    db_session.commit()

    # Deleting quiz must cascade-delete question, attempt, and answer
    db_session.delete(quiz)
    db_session.commit()

    assert db_session.get(QuizQuestion, q.id) is None
    assert db_session.get(QuizAttempt, attempt.id) is None
    assert db_session.get(QuizAnswer, ans.id) is None


def test_check_constraint_quiz_status(db_session):
    user = _create_user(db_session)
    cls = _create_class(db_session)

    with pytest.raises(IntegrityError):
        quiz = Quiz(
            id=uuid.uuid4(),
            class_id=cls.id,
            creator_id=user.id,
            title="Bad Status",
            status="INVALID_STATUS",
        )
        db_session.add(quiz)
        db_session.commit()


def test_check_constraint_question_type(db_session):
    user = _create_user(db_session)
    cls = _create_class(db_session)

    quiz = Quiz(id=uuid.uuid4(), class_id=cls.id, creator_id=user.id, title="Q Test")
    db_session.add(quiz)
    db_session.flush()

    with pytest.raises(IntegrityError):
        q = QuizQuestion(
            id=uuid.uuid4(),
            quiz_id=quiz.id,
            question_type="ESSAY_LONG",
            question="Tell me a story",
            difficulty="REMEMBER",
        )
        db_session.add(q)
        db_session.commit()


def test_check_constraint_question_difficulty_bloom_level(db_session):
    user = _create_user(db_session)
    cls = _create_class(db_session)

    quiz = Quiz(id=uuid.uuid4(), class_id=cls.id, creator_id=user.id, title="Q Test")
    db_session.add(quiz)
    db_session.flush()

    with pytest.raises(IntegrityError):
        q = QuizQuestion(
            id=uuid.uuid4(),
            quiz_id=quiz.id,
            question_type="SINGLE_CHOICE",
            question="What is this?",
            difficulty="VERY_HARD",  # Not in REMEMBER, UNDERSTAND, APPLY
        )
        db_session.add(q)
        db_session.commit()


def test_check_constraint_positive_points(db_session):
    user = _create_user(db_session)
    cls = _create_class(db_session)

    quiz = Quiz(id=uuid.uuid4(), class_id=cls.id, creator_id=user.id, title="Q Test")
    db_session.add(quiz)
    db_session.flush()

    with pytest.raises(IntegrityError):
        q = QuizQuestion(
            id=uuid.uuid4(),
            quiz_id=quiz.id,
            question_type="SINGLE_CHOICE",
            question="What is this?",
            points=-1.0,
            difficulty="REMEMBER",
        )
        db_session.add(q)
        db_session.commit()


def test_check_constraint_option_label_and_unique(db_session):
    user = _create_user(db_session)
    cls = _create_class(db_session)

    quiz = Quiz(id=uuid.uuid4(), class_id=cls.id, creator_id=user.id, title="Q Test")
    db_session.add(quiz)
    db_session.flush()

    q = QuizQuestion(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        question_type="SINGLE_CHOICE",
        question="Select option",
        difficulty="REMEMBER",
    )
    db_session.add(q)
    db_session.flush()

    # Invalid label (G not allowed)
    with pytest.raises(IntegrityError):
        opt_bad = QuizOption(
            id=uuid.uuid4(),
            question_id=q.id,
            option_label="G",
            option_text="Option G",
        )
        db_session.add(opt_bad)
        db_session.commit()
    db_session.rollback()

    # Duplicate label (two A options for the same question)
    opt1 = QuizOption(
        id=uuid.uuid4(),
        question_id=q.id,
        option_label="A",
        option_text="First A",
    )
    opt2 = QuizOption(
        id=uuid.uuid4(),
        question_id=q.id,
        option_label="A",
        option_text="Second A",
    )
    db_session.add(opt1)
    db_session.flush()

    with pytest.raises(IntegrityError):
        db_session.add(opt2)
        db_session.commit()


def test_alembic_quiz_migration_upgrade_and_downgrade(tmp_path):

    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from alembic import command

    test_db = tmp_path / "migration_test.db"
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{test_db}")

    # 0001 creates today's metadata, so start from the revision just before
    # the quiz schema with only the tables that 0032/0033 reference.
    engine = create_engine(f"sqlite:///{test_db}")
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE "class" (id CHAR(32) PRIMARY KEY)'))
        conn.execute(text('CREATE TABLE "user" (id CHAR(32) PRIMARY KEY)'))
        conn.execute(text('CREATE TABLE "ticket" (id CHAR(32) PRIMARY KEY)'))
    engine.dispose()

    command.stamp(cfg, "0031_ticket_lifecycle")
    # Apply 0032_quiz_schema and 0033_ticket_reopened_at.
    command.upgrade(cfg, "0034_ticket_escalation")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "0034_ticket_escalation")

