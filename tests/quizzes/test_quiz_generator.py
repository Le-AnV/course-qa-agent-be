"""Unit and integration tests for Quiz Generator blueprint sampling and skill radar (AI-149 / AI-151)."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from types import SimpleNamespace

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
from app.schemas.quiz import BlueprintSkillCell, QuizGenerateBlueprintRequest
from app.services import question_bank_service
from app.services.quiz_excel import FILL_IN_BLANK_ANSWERS_KEY
from app.services.quiz_generator_service import (
    compute_skill_radar_report,
    generate_quiz_from_blueprint,
    get_blueprint_availability,
)
from app.services.quiz_validation import MAX_REGENERATION_ATTEMPTS


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

    # Populate BankQuestions across multiple Dreyfus levels. Each question is a
    # genuinely different prompt: the AI-150 validator rejects near duplicates,
    # so reusing one template with only the question number changed would make
    # the fixture itself invalid.
    question_templates = [
        "Explain the central purpose of {code} ({name}) when planning a {level} project.",
        "A team debates {code} ({name}) alternatives for a {level} system. Which trade-off matters most?",
        "Describe one failure mode that {code} ({name}) is designed to prevent at {level}.",
        "How would an engineer verify that a {code} ({name}) decision is sound at {level}?",
        "During a code review, a junior engineer asks why {code} ({name}) matters for {level} work. What evidence should the reviewer request?",
        "Which observable symptom suggests that a {code} ({name}) rule was ignored by a {level} developer?",
        "Sketch an acceptance test that proves {code} ({name}) guidance was followed in a {level} deliverable.",
        "What assumption about {code} ({name}) most often breaks when a {level} team scales a prototype?",
        "Compare two competing {code} ({name}) strategies for a {level} rollout and state the decisive constraint.",
        "A production incident traces back to {code} ({name}). For a {level} responder, what is the first diagnostic step?",
        "Which metric would you instrument to confirm that {code} ({name}) practices hold at {level}?",
        "Given conflicting stakeholder requests, how should a {level} lead defend a {code} ({name}) decision?",
    ]
    level_offsets = {"NOVICE": 0, "BEGINNER": 4, "COMPETENT": 8}
    for s in (skill_oop, skill_sql):
        for lvl in ("NOVICE", "BEGINNER", "COMPETENT"):
            for i in range(4):
                template = question_templates[level_offsets[lvl] + i]
                bq = BankQuestion(
                    id=uuid.uuid4(),
                    class_id=clazz.id,
                    skill_id=s.id,
                    question_type="SINGLE_CHOICE",
                    question=template.format(code=s.code, name=s.name, level=lvl),
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
                        option_text=f"{s.code} {lvl} {i + 1} answer {label}",
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


@pytest.fixture()
def blueprint_minimal(db_session: Session):
    """A class with a teacher and one skill, but no Question Bank rows yet."""
    trainer_role = db_session.query(Role).filter(Role.name == "TRAINER").first()
    if not trainer_role:
        trainer_role = Role(name="TRAINER")
        db_session.add(trainer_role)
        db_session.flush()

    teacher = User(
        full_name="Teacher Minimal",
        email="teacher_minimal@example.com",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    db_session.add(teacher)
    db_session.flush()

    clazz = Class(
        trainer_id=teacher.id,
        name="Validated Blueprint Class",
        code="VB101",
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    db_session.add(clazz)
    db_session.flush()

    skill = Skill(
        id=uuid.uuid4(),
        class_id=clazz.id,
        code="VALIDATION",
        name="Validation Engineering",
        category="PROGRAMMING",
    )
    db_session.add(skill)
    db_session.commit()
    return {"teacher": teacher, "class": clazz, "skill": skill}


def _add_skill(db: Session, clazz: Class, code: str, name: str) -> Skill:
    skill = Skill(
        id=uuid.uuid4(),
        class_id=clazz.id,
        code=code,
        name=name,
        category="PROGRAMMING",
    )
    db.add(skill)
    db.commit()
    return skill


def _add_bank_question(
    db: Session,
    *,
    clazz: Class,
    skill: Skill,
    question: str,
    difficulty: str = "NOVICE",
    question_type: str = "SINGLE_CHOICE",
    option_texts: tuple[str, ...] = ("alpha", "beta", "gamma", "delta"),
    correct_index: int = 0,
    grading_config: dict | None = None,
) -> BankQuestion:
    bank_question = BankQuestion(
        id=uuid.uuid4(),
        class_id=clazz.id,
        skill_id=skill.id,
        question_type=question_type,
        question=question,
        points=1.0,
        difficulty=difficulty,
        grading_config=grading_config,
    )
    db.add(bank_question)
    db.flush()
    if question_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
        for index, text in enumerate(option_texts):
            db.add(
                BankQuestionOption(
                    id=uuid.uuid4(),
                    question_id=bank_question.id,
                    option_label=chr(65 + index),
                    option_text=text,
                    is_correct=(index == correct_index),
                    order_index=index,
                )
            )
    db.flush()
    return bank_question


def _fake_option(texts: tuple[str, ...], correct_index: int = 0):
    return [
        SimpleNamespace(
            option_label=chr(65 + index),
            option_text=text,
            is_correct=(index == correct_index),
        )
        for index, text in enumerate(texts)
    ]


def _fake_bank_question(
    *,
    class_id,
    skill_id,
    question: str,
    difficulty: str = "NOVICE",
    question_type: str = "SINGLE_CHOICE",
    options=None,
    confidence=None,
    validation_flags=None,
    grading_config=None,
):
    return SimpleNamespace(
        class_id=class_id,
        skill_id=skill_id,
        question_type=question_type,
        question=question,
        points=1.0,
        difficulty=difficulty,
        explanation=None,
        rubric=None,
        source_reference=None,
        grading_config=grading_config,
        options=list(options or []),
        confidence=confidence,
        validation_flags=validation_flags,
    )


def _blueprint_request(
    cells: list[BlueprintSkillCell],
    *,
    auto_generate_missing: bool = False,
    title: str = "Validated Blueprint Quiz",
    shuffle_questions: bool = False,
    shuffle_options: bool = False,
) -> QuizGenerateBlueprintRequest:
    return QuizGenerateBlueprintRequest(
        title=title,
        target_skill_ids=[cells[0].skill_id],
        target_levels=[cells[0].difficulty],
        questions_per_level=1,
        custom_distribution=cells,
        auto_generate_missing=auto_generate_missing,
        shuffle_questions=shuffle_questions,
        shuffle_options=shuffle_options,
    )


class _GeneratorSpy:
    """Record generator calls so auto_generate_missing=False can be proven offline."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("auto_generate_missing=False must not call the model generator")


def test_blueprint_endpoint_rejects_invalid_bank_question(
    client, db_session, blueprint_minimal, monkeypatch
):
    data = blueprint_minimal
    clazz = data["class"]
    teacher = data["teacher"]
    skill = data["skill"]
    spy = _GeneratorSpy()
    monkeypatch.setattr(question_bank_service, "generate_questions_from_documents", spy)

    _add_bank_question(
        db_session,
        clazz=clazz,
        skill=skill,
        question="Which statement about duplicated option text is correct?",
        option_texts=("dup", "DUP", "gamma", "delta"),
    )

    token = create_access_token(str(teacher.id), "TRAINER")
    res = client.post(
        f"/api/classes/{clazz.id}/quizzes/generate-blueprint",
        json={
            "title": "Invalid Bank Quiz",
            "target_skill_ids": [str(skill.id)],
            "target_levels": ["NOVICE"],
            "questions_per_level": 1,
            "auto_generate_missing": False,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert res.status_code == 400
    assert spy.calls == 0
    assert db_session.query(Quiz).count() == 0
    assert db_session.query(QuizQuestion).count() == 0


def test_blueprint_skips_invalid_bank_question_and_uses_next_valid(
    db_session, blueprint_minimal, monkeypatch
):
    data = blueprint_minimal
    clazz = data["class"]
    teacher = data["teacher"]
    skill = data["skill"]
    spy = _GeneratorSpy()
    monkeypatch.setattr(question_bank_service, "generate_questions_from_documents", spy)

    invalid_question = "Which statement about broken option payloads is correct?"
    valid_question = "Which answer explains a sound validation strategy?"
    _add_bank_question(
        db_session,
        clazz=clazz,
        skill=skill,
        question=invalid_question,
        option_texts=("same", "SAME", "gamma", "delta"),
    )
    _add_bank_question(
        db_session,
        clazz=clazz,
        skill=skill,
        question=valid_question,
    )
    monkeypatch.setattr(
        "app.services.quiz_generator_service.random.shuffle",
        lambda items: items.sort(key=lambda item: item.question != invalid_question),
    )

    quiz = generate_quiz_from_blueprint(
        db=db_session,
        class_id=clazz.id,
        req=_blueprint_request([BlueprintSkillCell(skill_id=skill.id, difficulty="NOVICE", count=1)]),
        user_id=teacher.id,
    )

    questions = db_session.query(QuizQuestion).filter(QuizQuestion.quiz_id == quiz.id).all()
    assert spy.calls == 0
    assert len(questions) == 1
    assert questions[0].question == valid_question


def test_blueprint_rejects_duplicate_bank_questions_with_different_ids(
    db_session, blueprint_minimal, monkeypatch
):
    data = blueprint_minimal
    clazz = data["class"]
    teacher = data["teacher"]
    skill = data["skill"]
    spy = _GeneratorSpy()
    monkeypatch.setattr(question_bank_service, "generate_questions_from_documents", spy)

    duplicate_text = "Which principle keeps duplicate bank rows out of a draft?"
    _add_bank_question(db_session, clazz=clazz, skill=skill, question=duplicate_text)
    _add_bank_question(db_session, clazz=clazz, skill=skill, question=duplicate_text)

    with pytest.raises(ValueError):
        generate_quiz_from_blueprint(
            db=db_session,
            class_id=clazz.id,
            req=_blueprint_request(
                [BlueprintSkillCell(skill_id=skill.id, difficulty="NOVICE", count=2)]
            ),
            user_id=teacher.id,
        )

    assert spy.calls == 0
    assert db_session.query(Quiz).count() == 0
    assert db_session.query(QuizQuestion).count() == 0


def test_blueprint_blocks_cross_cell_duplicate(
    db_session, blueprint_minimal, monkeypatch
):
    data = blueprint_minimal
    clazz = data["class"]
    teacher = data["teacher"]
    skill_one = data["skill"]
    skill_two = _add_skill(db_session, clazz, "SECONDARY", "Secondary Skill")
    spy = _GeneratorSpy()
    monkeypatch.setattr(question_bank_service, "generate_questions_from_documents", spy)

    duplicate_text = "Which shared question must not appear in two blueprint cells?"
    _add_bank_question(db_session, clazz=clazz, skill=skill_one, question=duplicate_text)
    _add_bank_question(db_session, clazz=clazz, skill=skill_two, question=duplicate_text)

    with pytest.raises(ValueError):
        generate_quiz_from_blueprint(
            db=db_session,
            class_id=clazz.id,
            req=_blueprint_request(
                [
                    BlueprintSkillCell(skill_id=skill_one.id, difficulty="NOVICE", count=1),
                    BlueprintSkillCell(skill_id=skill_two.id, difficulty="NOVICE", count=1),
                ]
            ),
            user_id=teacher.id,
        )

    assert spy.calls == 0
    assert db_session.query(Quiz).count() == 0


def test_blueprint_refills_cross_cell_duplicate_from_same_cell(
    db_session, blueprint_minimal, monkeypatch
):
    data = blueprint_minimal
    clazz = data["class"]
    teacher = data["teacher"]
    skill_one = data["skill"]
    skill_two = _add_skill(db_session, clazz, "SECONDARY", "Secondary Skill")
    spy = _GeneratorSpy()
    monkeypatch.setattr(question_bank_service, "generate_questions_from_documents", spy)

    duplicate_text = "Which shared question must be replaced inside its cell?"
    replacement_text = "Which distinct question should refill the second cell?"
    _add_bank_question(db_session, clazz=clazz, skill=skill_one, question=duplicate_text)
    _add_bank_question(db_session, clazz=clazz, skill=skill_two, question=duplicate_text)
    _add_bank_question(db_session, clazz=clazz, skill=skill_two, question=replacement_text)
    monkeypatch.setattr(
        "app.services.quiz_generator_service.random.shuffle",
        lambda items: items.sort(key=lambda item: item.question != duplicate_text),
    )

    quiz = generate_quiz_from_blueprint(
        db=db_session,
        class_id=clazz.id,
        req=_blueprint_request(
            [
                BlueprintSkillCell(skill_id=skill_one.id, difficulty="NOVICE", count=1),
                BlueprintSkillCell(skill_id=skill_two.id, difficulty="NOVICE", count=1),
            ]
        ),
        user_id=teacher.id,
    )

    questions = db_session.query(QuizQuestion).filter(QuizQuestion.quiz_id == quiz.id).all()
    assert spy.calls == 0
    assert len(questions) == 2
    by_skill = {question.skill_id: question for question in questions}
    assert by_skill[skill_one.id].question == duplicate_text
    assert by_skill[skill_two.id].question == replacement_text


def test_blueprint_generated_fallback_retries_bad_candidates(
    db_session, blueprint_minimal, monkeypatch
):
    data = blueprint_minimal
    clazz = data["class"]
    teacher = data["teacher"]
    skill = data["skill"]
    other_skill = _add_skill(db_session, clazz, "OTHER", "Other Skill")

    bank_question = _add_bank_question(
        db_session,
        clazz=clazz,
        skill=skill,
        question="Which bank question anchors the retry scenario?",
    )
    calls = {"count": 0}

    def fake_generate(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return [
                _fake_bank_question(
                    class_id=clazz.id,
                    skill_id=skill.id,
                    question="Malformed generated payload with only one option?",
                    options=_fake_option(("only",), correct_index=0),
                ),
                _fake_bank_question(
                    class_id=clazz.id,
                    skill_id=other_skill.id,
                    question="Which generated question belongs to the wrong cell?",
                    options=_fake_option(("wrong", "cell", "gamma", "delta")),
                ),
                _fake_bank_question(
                    class_id=clazz.id,
                    skill_id=skill.id,
                    question=bank_question.question,
                    options=_fake_option(("dup", "beta", "gamma", "delta")),
                ),
            ]
        return [
            _fake_bank_question(
                class_id=clazz.id,
                skill_id=skill.id,
                question="Which generated replacement validates the first missing slot?",
                options=_fake_option(("first", "beta", "gamma", "delta")),
            ),
            _fake_bank_question(
                class_id=clazz.id,
                skill_id=skill.id,
                question="What evidence proves the second generated slot is distinct?",
                options=_fake_option(("second", "beta", "gamma", "delta")),
            ),
        ]

    monkeypatch.setattr(question_bank_service, "generate_questions_from_documents", fake_generate)

    quiz = generate_quiz_from_blueprint(
        db=db_session,
        class_id=clazz.id,
        req=_blueprint_request(
            [BlueprintSkillCell(skill_id=skill.id, difficulty="NOVICE", count=3)],
            auto_generate_missing=True,
        ),
        user_id=teacher.id,
    )

    questions = db_session.query(QuizQuestion).filter(QuizQuestion.quiz_id == quiz.id).all()
    assert calls["count"] == 2
    assert len(questions) == 3
    assert {question.skill_id for question in questions} == {skill.id}
    assert {question.difficulty for question in questions} == {"NOVICE"}
    assert {question.question for question in questions} == {
        bank_question.question,
        "Which generated replacement validates the first missing slot?",
        "What evidence proves the second generated slot is distinct?",
    }


def test_blueprint_exhausted_generation_leaves_no_partial_quiz(
    db_session, blueprint_minimal, monkeypatch
):
    data = blueprint_minimal
    clazz = data["class"]
    teacher = data["teacher"]
    skill = data["skill"]
    calls = {"count": 0}

    def fake_generate(*args, **kwargs):
        calls["count"] += 1
        return [
            _fake_bank_question(
                class_id=clazz.id,
                skill_id=skill.id,
                question="Malformed generated payload that must never be saved?",
                options=_fake_option(("only",), correct_index=0),
            )
        ]

    monkeypatch.setattr(question_bank_service, "generate_questions_from_documents", fake_generate)

    with pytest.raises(ValueError):
        generate_quiz_from_blueprint(
            db=db_session,
            class_id=clazz.id,
            req=_blueprint_request(
                [BlueprintSkillCell(skill_id=skill.id, difficulty="NOVICE", count=1)],
                auto_generate_missing=True,
            ),
            user_id=teacher.id,
        )

    assert calls["count"] == MAX_REGENERATION_ATTEMPTS
    assert db_session.query(Quiz).count() == 0
    assert db_session.query(QuizQuestion).count() == 0


def test_blueprint_keeps_low_confidence_generated_question_with_flag(
    db_session, blueprint_minimal, monkeypatch
):
    data = blueprint_minimal
    clazz = data["class"]
    teacher = data["teacher"]
    skill = data["skill"]

    def fake_generate(*args, **kwargs):
        return [
            _fake_bank_question(
                class_id=clazz.id,
                skill_id=skill.id,
                question="Which uncertain generated answer should still reach review?",
                options=_fake_option(("uncertain", "beta", "gamma", "delta")),
                confidence=0.4,
            )
        ]

    monkeypatch.setattr(question_bank_service, "generate_questions_from_documents", fake_generate)

    quiz = generate_quiz_from_blueprint(
        db=db_session,
        class_id=clazz.id,
        req=_blueprint_request(
            [BlueprintSkillCell(skill_id=skill.id, difficulty="NOVICE", count=1)],
            auto_generate_missing=True,
        ),
        user_id=teacher.id,
    )

    question = db_session.query(QuizQuestion).filter(QuizQuestion.quiz_id == quiz.id).one()
    assert question.confidence == 0.4
    assert question.validation_flags == ["LOW_CONFIDENCE"]


def test_blueprint_preserves_fill_in_blank_answers_and_skill_mapping(
    db_session, blueprint_minimal, monkeypatch
):
    data = blueprint_minimal
    clazz = data["class"]
    teacher = data["teacher"]
    skill = data["skill"]
    spy = _GeneratorSpy()
    monkeypatch.setattr(question_bank_service, "generate_questions_from_documents", spy)

    _add_bank_question(
        db_session,
        clazz=clazz,
        skill=skill,
        question="The validated answer is ____.",
        question_type="FILL_IN_BLANK",
        grading_config={FILL_IN_BLANK_ANSWERS_KEY: ["42"]},
    )

    quiz = generate_quiz_from_blueprint(
        db=db_session,
        class_id=clazz.id,
        req=_blueprint_request(
            [BlueprintSkillCell(skill_id=skill.id, difficulty="NOVICE", count=1)]
        ),
        user_id=teacher.id,
    )

    question = db_session.query(QuizQuestion).filter(QuizQuestion.quiz_id == quiz.id).one()
    assert spy.calls == 0
    assert question.question_type == "FILL_IN_BLANK"
    assert question.skill_id == skill.id
    assert question.difficulty == "NOVICE"
    assert question.grading_config[FILL_IN_BLANK_ANSWERS_KEY] == ["42"]


def test_blueprint_happy_path_keeps_counts_and_cell_mapping(
    db_session, seed_class_and_skills
):
    data = seed_class_and_skills
    clazz = data["class"]
    teacher = data["teacher"]
    skill_oop = data["skill_oop"]
    skill_sql = data["skill_sql"]

    cells = [
        BlueprintSkillCell(skill_id=skill_oop.id, difficulty="NOVICE", count=2),
        BlueprintSkillCell(skill_id=skill_oop.id, difficulty="BEGINNER", count=2),
        BlueprintSkillCell(skill_id=skill_sql.id, difficulty="NOVICE", count=2),
        BlueprintSkillCell(skill_id=skill_sql.id, difficulty="BEGINNER", count=2),
    ]
    quiz = generate_quiz_from_blueprint(
        db=db_session,
        class_id=clazz.id,
        req=_blueprint_request(cells, title="Mapped Blueprint Quiz"),
        user_id=teacher.id,
    )

    questions = db_session.query(QuizQuestion).filter(QuizQuestion.quiz_id == quiz.id).all()
    assert len(questions) == 8
    counts: dict[tuple[uuid.UUID, str], int] = {}
    for question in questions:
        key = (question.skill_id, question.difficulty)
        counts[key] = counts.get(key, 0) + 1
    assert counts == {
        (skill_oop.id, "NOVICE"): 2,
        (skill_oop.id, "BEGINNER"): 2,
        (skill_sql.id, "NOVICE"): 2,
        (skill_sql.id, "BEGINNER"): 2,
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

