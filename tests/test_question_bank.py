"""Unit tests for Question Bank repository, Excel export/import, and generation prompts (AI-160)."""

from __future__ import annotations

import uuid

import openpyxl
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.core.prompt_manager import get_prompt_manager
from app.models.course import Class
from app.models.question_bank import BankQuestion, BankQuestionOption
from app.models.skill import Skill
from app.models.user import Role, User
from app.schemas.question_bank import (
    BankQuestionCreate,
    BankQuestionOptionCreate,
    BankQuestionUpdate,
)
from app.services import question_bank_service


@pytest.fixture
def db_session():
    """Isolated in-memory SQLite database for question bank tests."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session: Session = session_factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed_class_and_skills(db: Session) -> tuple[Class, Skill, User]:
    role = Role(id=uuid.uuid4(), name="TEACHER")
    db.add(role)
    db.flush()

    teacher = User(
        id=uuid.uuid4(),
        email=f"teacher_{uuid.uuid4().hex[:6]}@example.com",
        full_name="Dr. Alan Turing",
        role_id=role.id,
        status="ACTIVE",
    )
    db.add(teacher)
    db.flush()

    cls = Class(
        id=uuid.uuid4(),
        trainer_id=teacher.id,
        code="CS201",
        name="Advanced C++ Programming",
        credits=3,
        semester="Spring",
        academic_year="2026",
        status="ACTIVE",
    )
    db.add(cls)
    db.flush()

    skill = Skill(
        id=uuid.uuid4(),
        class_id=cls.id,
        code="CPP_POINTERS",
        name="Pointers & Memory Architecture",
        category="CORE_SYNTAX",
        description="Understanding raw pointers, smart pointers, and heap allocations.",
    )
    db.add(skill)
    db.commit()

    return cls, skill, teacher


def test_question_bank_crud_and_counts(db_session: Session):
    cls, skill, teacher = _seed_class_and_skills(db_session)

    # 1. Create SINGLE_CHOICE question
    q1_payload = BankQuestionCreate(
        question_type="SINGLE_CHOICE",
        question="Which operator accesses members through a pointer?",
        points=1.0,
        difficulty="NOVICE",
        skill_id=skill.id,
        explanation="The arrow operator (->) is syntactic sugar for (*ptr).member.",
        options=[
            BankQuestionOptionCreate(option_label="A", option_text="Dot operator (.)", is_correct=False, order_index=0),
            BankQuestionOptionCreate(option_label="B", option_text="Arrow operator (->)", is_correct=True, order_index=1),
            BankQuestionOptionCreate(option_label="C", option_text="Scope resolution (::)", is_correct=False, order_index=2),
            BankQuestionOptionCreate(option_label="D", option_text="Address-of (&)", is_correct=False, order_index=3),
        ],
    )
    q1 = question_bank_service.create_bank_question(db_session, cls.id, q1_payload, teacher.id)
    assert q1.id is not None
    assert len(q1.options) == 4

    # 2. Create FILL_IN_BLANK question
    q2_payload = BankQuestionCreate(
        question_type="FILL_IN_BLANK",
        question="What keyword frees heap memory allocated with new in C++?",
        points=2.0,
        difficulty="BEGINNER",
        skill_id=skill.id,
        correct_answers=["delete", "delete operator"],
        explanation="The delete operator deallocates dynamic memory.",
    )
    q2 = question_bank_service.create_bank_question(db_session, cls.id, q2_payload, teacher.id)
    assert q2.id is not None
    assert q2.grading_config is not None
    assert "delete" in q2.grading_config.get("correct_answers", [])

    # 3. List questions & verify statistics
    res = question_bank_service.list_bank_questions(db_session, cls.id)
    assert res.total == 2
    assert res.counts_by_type.get("SINGLE_CHOICE") == 1
    assert res.counts_by_type.get("FILL_IN_BLANK") == 1
    assert res.counts_by_difficulty.get("NOVICE") == 1
    assert res.counts_by_difficulty.get("BEGINNER") == 1

    # 4. Filter by type
    single_res = question_bank_service.list_bank_questions(db_session, cls.id, question_type="SINGLE_CHOICE")
    assert single_res.total == 1
    assert single_res.items[0].question_type == "SINGLE_CHOICE"

    # 5. Search text
    search_res = question_bank_service.list_bank_questions(db_session, cls.id, search="arrow")
    assert search_res.total == 1
    assert search_res.items[0].id == q1.id

    # 6. Update question
    update_payload = BankQuestionUpdate(
        points=2.5,
        difficulty="COMPETENT",
        status="DRAFT",
    )
    updated = question_bank_service.update_bank_question(db_session, cls.id, q1.id, update_payload)
    assert updated.points == 2.5
    assert updated.difficulty == "COMPETENT"
    assert updated.status == "DRAFT"

    # 7. Bulk update status
    count = question_bank_service.bulk_update_status(db_session, cls.id, [q1.id, q2.id], "ARCHIVED")
    assert count == 2
    reloaded = question_bank_service.get_bank_question(db_session, cls.id, q1.id)
    assert reloaded.status == "ARCHIVED"

    # 8. Delete question
    deleted = question_bank_service.delete_bank_question(db_session, cls.id, q1.id)
    assert deleted is True
    assert question_bank_service.get_bank_question(db_session, cls.id, q1.id) is None


def test_question_bank_excel_export_and_import(db_session: Session):
    cls, skill, teacher = _seed_class_and_skills(db_session)

    # Seed 1 single choice and 1 fill in blank question
    q1 = BankQuestion(
        id=uuid.uuid4(),
        class_id=cls.id,
        skill_id=skill.id,
        created_by_id=teacher.id,
        question_type="SINGLE_CHOICE",
        question="Which operator accesses members through a pointer?",
        points=1.0,
        difficulty="NOVICE",
        explanation="Arrow operator.",
        source_reference="Pointers Lecture",
        status="APPROVED",
    )
    db_session.add(q1)
    db_session.flush()

    for idx, (lbl, text, corr) in enumerate([
        ("A", "Dot (.)", False),
        ("B", "Arrow (->)", True),
        ("C", "Colons (::)", False),
    ]):
        db_session.add(
            BankQuestionOption(
                id=uuid.uuid4(),
                question_id=q1.id,
                option_label=lbl,
                option_text=text,
                is_correct=corr,
                order_index=idx,
            )
        )

    q2 = BankQuestion(
        id=uuid.uuid4(),
        class_id=cls.id,
        skill_id=skill.id,
        created_by_id=teacher.id,
        question_type="FILL_IN_BLANK",
        question="Which keyword frees dynamically allocated memory?",
        points=1.0,
        difficulty="BEGINNER",
        explanation="delete keyword frees heap memory.",
        rubric="Accept delete or delete[]",
        grading_config={"correct_answers": ["delete", "delete[]"]},
        source_reference="Memory Allocation",
        status="APPROVED",
    )
    db_session.add(q2)
    db_session.commit()

    # Export to Excel stream
    excel_stream = question_bank_service.export_bank_to_excel(db_session, cls.id)
    assert excel_stream is not None

    wb = openpyxl.load_workbook(excel_stream, data_only=True)
    assert "Questions" in wb.sheetnames
    assert "Instructions" in wb.sheetnames

    ws = wb["Questions"]
    header = [cell.value for cell in ws[1]]
    expected_header = [
        "Question Type", "Question", "Option A", "Option B", "Option C", "Option D",
        "Option E", "Option F", "Correct Answer(s)", "Points", "Difficulty",
        "Explanation", "Rubric", "Source Reference"
    ]
    assert header == expected_header

    # Row 2 (Single choice)
    row2 = [cell.value for cell in ws[2]]
    assert row2[0] == "SINGLE_CHOICE"
    assert row2[1] == "Which operator accesses members through a pointer?"
    assert row2[2] == "Dot (.)"
    assert row2[3] == "Arrow (->)"
    assert row2[8] == "B"  # Correct Answer

    # Row 3 (Fill in blank)
    row3 = [cell.value for cell in ws[3]]
    assert row3[0] == "FILL_IN_BLANK"
    assert row3[1] == "Which keyword frees dynamically allocated memory?"
    assert "delete" in str(row3[8])  # Correct Answer(s) separated by pipe

    # Now test importing this workbook back into another class
    cls2 = Class(
        id=uuid.uuid4(),
        trainer_id=teacher.id,
        code="CS202",
        name="Data Structures",
        credits=3,
        semester="Fall",
        academic_year="2026",
        status="ACTIVE",
    )
    db_session.add(cls2)
    db_session.commit()

    excel_stream.seek(0)
    file_bytes = excel_stream.read()
    imported_count, _warnings = question_bank_service.import_bank_from_excel(
        db_session, cls2.id, file_bytes, teacher.id
    )
    assert imported_count == 2

    # Check imported items in cls2
    imported_list = question_bank_service.list_bank_questions(db_session, cls2.id)
    assert imported_list.total == 2
    for item in imported_list.items:
        assert item.source_type == "EXCEL_IMPORT"


def test_question_bank_prompt_templates_validity():
    """Verify that Question Bank and Misconception prompt YAML files load and contain valid placeholders."""
    prompt_mgr = get_prompt_manager()

    # Question Bank generation prompt
    gen_prompt = prompt_mgr.get("question-bank-gen-prompt")
    assert gen_prompt is not None
    assert "{class_name}" in gen_prompt["user-prompt"]
    assert "{skills_context}" in gen_prompt["user-prompt"]
    assert "{difficulty_breakdown}" in gen_prompt["user-prompt"]
    assert "{question_types}" in gen_prompt["user-prompt"]
    assert "{num_questions}" in gen_prompt["user-prompt"]

    # Misconception probing prompt
    probe_prompt = prompt_mgr.get("misconception-probing-prompt")
    assert probe_prompt is not None
    assert "{class_name}" in probe_prompt["user-prompt"]
    assert "{difficulty_breakdown}" in probe_prompt["user-prompt"]
    assert "{student_inquiries}" in probe_prompt["user-prompt"]
    assert "{num_questions}" in probe_prompt["user-prompt"]
