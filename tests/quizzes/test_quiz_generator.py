"""Unit and integration tests for Quiz Generator blueprint sampling and skill radar (AI-149 / AI-151)."""

from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.config import settings
from app.core.database import Base, reset_engine
from app.core.deps import get_db
from app.core.security import create_access_token
from app.main import app
from app.models.course import Class
from app.models.question_bank import BankQuestion, BankQuestionOption
from app.models.quiz import Quiz, QuizAnswer, QuizAttempt, QuizQuestion
from app.models.skill import Skill, SkillLevelRubric
from app.models.user import Role, User
from app.services.quiz_generator_service import (
    compute_skill_radar_report,
    get_blueprint_availability,
)


@pytest.fixture()
def db_session(monkeypatch) -> Generator[Session, None, None]:
    monkeypatch.setattr("app.services.reranker_service.warm_up", lambda: None)
    monkeypatch.setattr("app.main.seed_holidays", lambda db: None)
    import app.core.database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", "sqlite://")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    dbmod._db_url = "sqlite://"
    Base.metadata.create_all(bind=engine)

    with dbmod._SessionLocal() as session:
        yield session

    Base.metadata.drop_all(bind=engine)
    reset_engine()


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    def _override():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture()
def seed_class_and_skills(db_session: Session):
    trainer_role = db_session.query(Role).filter(Role.name == "TRAINER").first()
    if not trainer_role:
        trainer_role = Role(name="TRAINER")
        db_session.add(trainer_role)
        db_session.flush()

    teacher = User(
        full_name="Teacher Gen",
        email="teacher_gen@example.com",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    db_session.add(teacher)
    db_session.flush()

    clazz = Class(
        trainer_id=teacher.id,
        name="Software Architecture",
        code="CS401",
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    db_session.add(clazz)
    db_session.flush()

    skill_oop = Skill(
        id=uuid.uuid4(),
        class_id=clazz.id,
        code="OOP_DESIGN",
        name="Object-Oriented Design",
        category="PROGRAMMING",
    )
    skill_sql = Skill(
        id=uuid.uuid4(),
        class_id=clazz.id,
        code="SQL_DATABASE",
        name="Relational Databases",
        category="DATABASE",
    )
    db_session.add_all([skill_oop, skill_sql])
    db_session.flush()

    for s in (skill_oop, skill_sql):
        for lvl, th in (("NOVICE", 80), ("BEGINNER", 75), ("COMPETENT", 70), ("PROFICIENT", 65), ("MASTER", 60)):
            r = SkillLevelRubric(
                id=uuid.uuid4(),
                skill_id=s.id,
                level=lvl,
                threshold_pct=th,
                description=f"Rubric criteria for {s.code} at {lvl}",
            )
            db_session.add(r)
    db_session.flush()

    # Populate BankQuestions across multiple Dreyfus levels
    for s in (skill_oop, skill_sql):
        for lvl in ("NOVICE", "BEGINNER", "COMPETENT"):
            for i in range(4):
                bq = BankQuestion(
                    id=uuid.uuid4(),
                    class_id=clazz.id,
                    skill_id=s.id,
                    question_type="SINGLE_CHOICE",
                    question=f"{s.code} {lvl} question #{i + 1}: What is the core concept?",
                    points=1.0,
                    difficulty=lvl,
                    explanation="Standard concept explanation",
                    rubric=f"Criteria for {lvl}",
                )
                db_session.add(bq)
                db_session.flush()

                for opt_idx, label in enumerate(["A", "B", "C", "D"]):
                    opt = BankQuestionOption(
                        id=uuid.uuid4(),
                        question_id=bq.id,
                        option_label=label,
                        option_text=f"Option {label} for question {i + 1}",
                        is_correct=(opt_idx == 0),
                        order_index=opt_idx,
                    )
                    db_session.add(opt)

    db_session.commit()
    return {
        "teacher": teacher,
        "class": clazz,
        "skill_oop": skill_oop,
        "skill_sql": skill_sql,
    }


def test_blueprint_availability_service_and_endpoint(client, db_session, seed_class_and_skills):
    data = seed_class_and_skills
    clazz = data["class"]
    teacher = data["teacher"]

    token = create_access_token(str(teacher.id), "TRAINER")
    headers = {"Authorization": f"Bearer {token}"}

    # Direct service test
    avail = get_blueprint_availability(db_session, clazz.id)
    assert avail.class_id == clazz.id
    assert avail.total_bank_questions == 24  # 2 skills * 3 levels * 4 questions
    assert len(avail.cells) == 10  # 2 skills * 5 Dreyfus levels

    # API route test
    res = client.get(f"/api/classes/{clazz.id}/quizzes/blueprint-matrix", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["total_bank_questions"] == 24
    assert len(body["cells"]) == 10


def test_generate_quiz_from_blueprint(client, db_session, seed_class_and_skills):
    data = seed_class_and_skills
    clazz = data["class"]
    teacher = data["teacher"]
    skill_oop = data["skill_oop"]
    skill_sql = data["skill_sql"]

    token = create_access_token(str(teacher.id), "TRAINER")
    headers = {"Authorization": f"Bearer {token}"}

    payload = {
        "title": "Midterm Architecture Exam",
        "description": "Standardized Blueprint Assessment",
        "time_limit_minutes": 45,
        "passing_score": 70.0,
        "target_skill_ids": [str(skill_oop.id), str(skill_sql.id)],
        "target_levels": ["NOVICE", "BEGINNER"],
        "questions_per_level": 2,
        "shuffle_questions": True,
        "shuffle_options": True,
        "auto_generate_missing": False,
    }

    res = client.post(
        f"/api/classes/{clazz.id}/quizzes/generate-blueprint",
        json=payload,
        headers=headers,
    )
    assert res.status_code == 201
    quiz_data = res.json()
    assert quiz_data["title"] == "Midterm Architecture Exam"
    assert quiz_data["status"] == "DRAFT"
    # 2 skills * 2 levels * 2 questions = 8 questions total
    assert quiz_data["question_count"] == 8
    assert len(quiz_data["questions"]) == 8

    # Verify each question has options with exactly 1 correct answer
    for q in quiz_data["questions"]:
        assert q["difficulty"] in ("NOVICE", "BEGINNER")
        assert len(q["options"]) == 4
        correct_count = sum(1 for o in q["options"] if o["is_correct"])
        assert correct_count == 1


def test_swap_or_regenerate_question(client, db_session, seed_class_and_skills):
    data = seed_class_and_skills
    clazz = data["class"]
    teacher = data["teacher"]
    skill_oop = data["skill_oop"]

    token = create_access_token(str(teacher.id), "TRAINER")
    headers = {"Authorization": f"Bearer {token}"}

    # Generate small 2-question quiz
    gen_res = client.post(
        f"/api/classes/{clazz.id}/quizzes/generate-blueprint",
        json={
            "title": "OOP Quiz",
            "target_skill_ids": [str(skill_oop.id)],
            "target_levels": ["NOVICE"],
            "questions_per_level": 1,
        },
        headers=headers,
    )
    assert gen_res.status_code == 201
    quiz_obj = gen_res.json()
    target_q = quiz_obj["questions"][0]

    # Call regenerate on target_q
    regen_res = client.post(
        f"/api/classes/{clazz.id}/quizzes/{quiz_obj['id']}/questions/{target_q['id']}/regenerate",
        headers=headers,
    )
    assert regen_res.status_code == 200
    updated_quiz = regen_res.json()
    new_q = updated_quiz["questions"][0]
    assert new_q["id"] == target_q["id"]  # Same question slot
    assert new_q["difficulty"] == "NOVICE"
    assert len(new_q["options"]) == 4


def test_compute_skill_radar_report(db_session, seed_class_and_skills):
    data = seed_class_and_skills
    clazz = data["class"]
    teacher = data["teacher"]
    skill_oop = data["skill_oop"]

    quiz = Quiz(
        id=uuid.uuid4(),
        class_id=clazz.id,
        creator_id=teacher.id,
        title="Diagnostic Quiz",
        status="PUBLISHED",
    )
    db_session.add(quiz)
    db_session.flush()

    # Create questions for Novice, Beginner, Competent
    qq_list = []
    for idx, lvl in enumerate(["NOVICE", "BEGINNER", "COMPETENT"]):
        for i in range(3):
            qq = QuizQuestion(
                id=uuid.uuid4(),
                quiz_id=quiz.id,
                skill_id=skill_oop.id,
                question_type="SINGLE_CHOICE",
                question=f"Q {lvl} {i}",
                difficulty=lvl,
                points=1.0,
                order_index=idx * 3 + i,
            )
            db_session.add(qq)
            qq_list.append(qq)
    db_session.flush()

    attempt = QuizAttempt(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        student_id=teacher.id,
        status="SUBMITTED",
        total_score=6.0,
    )
    db_session.add(attempt)
    db_session.flush()

    # Student answers 100% on Novice, 100% on Beginner, 0% on Competent
    answers_map = {}
    for qq in qq_list:
        score = 1.0 if qq.difficulty in ("NOVICE", "BEGINNER") else 0.0
        ans = QuizAnswer(
            id=uuid.uuid4(),
            attempt_id=attempt.id,
            question_id=qq.id,
            score_awarded=score,
        )
        db_session.add(ans)
        answers_map[qq.id] = ans
    db_session.flush()

    radar = compute_skill_radar_report(quiz, answers_map)
    assert len(radar) == 1
    item = radar[0]
    assert item.skill_code == "OOP_DESIGN"
    # Passed Novice & Beginner, blocked at Competent -> Achieved BEGINNER
    assert item.achieved_level == "BEGINNER"
    assert item.is_blocked is True
    assert item.blocked_at == "COMPETENT"


def test_show_solutions_flag_and_sanitization(db_session, seed_class_and_skills):
    from app.models.quiz import QuizOption
    from app.services.quiz_attempt_service import _result

    data = seed_class_and_skills
    clazz = data["class"]
    teacher = data["teacher"]
    skill_oop = data["skill_oop"]

    quiz = Quiz(
        id=uuid.uuid4(),
        class_id=clazz.id,
        creator_id=teacher.id,
        title="Hidden Solutions Exam",
        status="PUBLISHED",
        show_solutions_after_submission=False,
    )
    db_session.add(quiz)
    db_session.flush()

    qq = QuizQuestion(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        skill_id=skill_oop.id,
        question_type="SINGLE_CHOICE",
        question="Secret question?",
        difficulty="NOVICE",
        points=1.0,
        explanation="Secret explanation",
        order_index=0,
    )
    db_session.add(qq)
    db_session.flush()

    opt_correct = QuizOption(
        id=uuid.uuid4(),
        question_id=qq.id,
        option_label="A",
        option_text="Secret Answer",
        is_correct=True,
        order_index=0,
    )
    opt_wrong = QuizOption(
        id=uuid.uuid4(),
        question_id=qq.id,
        option_label="B",
        option_text="Distractor",
        is_correct=False,
        order_index=1,
    )
    db_session.add_all([opt_correct, opt_wrong])
    db_session.flush()

    attempt = QuizAttempt(
        id=uuid.uuid4(),
        quiz_id=quiz.id,
        student_id=teacher.id,
        status="SUBMITTED",
        total_score=1.0,
    )
    db_session.add(attempt)
    db_session.flush()

    result = _result(quiz, attempt)
    assert result.show_solutions_after_submission is False
    assert len(result.questions) == 1
    sanitized_q = result.questions[0]
    assert sanitized_q.explanation is None
    for opt in sanitized_q.options:
        assert opt.is_correct is False

