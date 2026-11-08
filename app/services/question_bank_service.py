"""Service layer for Question Bank management, AI generation, and Excel import/export (AI-160)."""

from __future__ import annotations

import io
import json
import logging
import re
import uuid
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.prompt_manager import get_prompt_manager
from app.models.chat import ChatMessage, ChatSession
from app.models.course import Class
from app.models.document import Document
from app.models.question_bank import (
    BANK_STATUSES,
    BankQuestion,
    BankQuestionOption,
)
from app.models.skill import Skill
from app.models.ticket import Ticket
from app.schemas.question_bank import (
    BankQuestionCreate,
    BankQuestionListResponse,
    BankQuestionOptionRead,
    BankQuestionRead,
    BankQuestionUpdate,
    DocumentQuestionGenRequest,
    MisconceptionGenRequest,
)
from app.services.llm.provider import get_llm_model
from app.services.quiz_excel import (
    FILL_IN_BLANK_ANSWERS_KEY,
    export_quiz_to_workbook,
    validate_and_parse_quiz_excel,
)

logger = logging.getLogger(__name__)


def _to_read_dto(q: BankQuestion) -> BankQuestionRead:
    """Map a SQLAlchemy BankQuestion to its Pydantic read schema."""
    options_read = [
        BankQuestionOptionRead(
            id=opt.id,
            question_id=opt.question_id,
            option_label=opt.option_label,
            option_text=opt.option_text,
            is_correct=opt.is_correct,
            order_index=opt.order_index,
        )
        for opt in (q.options or [])
    ]

    fill_answers: list[str] = []
    if q.question_type == "FILL_IN_BLANK" and q.grading_config:
        raw = q.grading_config.get(FILL_IN_BLANK_ANSWERS_KEY)
        if isinstance(raw, list):
            fill_answers = [str(x) for x in raw]
        elif isinstance(raw, str):
            fill_answers = [raw]

    return BankQuestionRead(
        id=q.id,
        class_id=q.class_id,
        skill_id=q.skill_id,
        skill_code=q.skill.code if q.skill else None,
        skill_name=q.skill.name if q.skill else None,
        created_by_id=q.created_by_id,
        question_type=q.question_type,
        question=q.question,
        points=q.points,
        difficulty=q.difficulty,
        explanation=q.explanation,
        rubric=q.rubric,
        source_type=q.source_type,
        source_document_id=q.source_document_id,
        source_document_name=q.source_document.file_name if q.source_document else None,
        source_reference=q.source_reference,
        status=q.status,
        grading_config=q.grading_config,
        correct_answers=fill_answers if fill_answers else None,
        options=options_read,
        created_at=q.created_at,
        updated_at=q.updated_at,
    )


def list_bank_questions(
    db: Session,
    class_id: uuid.UUID,
    skill_id: uuid.UUID | None = None,
    question_type: str | None = None,
    difficulty: str | None = None,
    status: str | None = None,
    source_type: str | None = None,
    search: str | None = None,
    skip: int = 0,
    limit: int = 100,
) -> BankQuestionListResponse:
    """Retrieve filtered, paginated question repository items with aggregated breakdown statistics."""
    query = (
        select(BankQuestion)
        .where(BankQuestion.class_id == class_id)
        .options(
            selectinload(BankQuestion.options),
            selectinload(BankQuestion.skill),
            selectinload(BankQuestion.source_document),
        )
    )

    if skill_id:
        query = query.where(BankQuestion.skill_id == skill_id)
    if question_type:
        query = query.where(BankQuestion.question_type == question_type.upper())
    if difficulty:
        query = query.where(BankQuestion.difficulty == difficulty.upper())
    if status and status.upper() != "ALL":
        query = query.where(BankQuestion.status == status.upper())
    if source_type:
        query = query.where(BankQuestion.source_type == source_type.upper())
    if search and search.strip():
        term = f"%{search.strip()}%"
        query = query.where(
            BankQuestion.question.ilike(term)
            | BankQuestion.explanation.ilike(term)
            | BankQuestion.source_reference.ilike(term)
        )

    # Order by updated_at descending
    query = query.order_by(BankQuestion.updated_at.desc())

    all_matches = list(db.scalars(query).all())
    total = len(all_matches)
    paginated_items = all_matches[skip : skip + limit]

    # Calculate breakdown statistics
    counts_by_difficulty: dict[str, int] = {}
    counts_by_type: dict[str, int] = {}
    counts_by_status: dict[str, int] = {}

    for q in all_matches:
        counts_by_difficulty[q.difficulty] = counts_by_difficulty.get(q.difficulty, 0) + 1
        counts_by_type[q.question_type] = counts_by_type.get(q.question_type, 0) + 1
        counts_by_status[q.status] = counts_by_status.get(q.status, 0) + 1

    return BankQuestionListResponse(
        total=total,
        items=[_to_read_dto(q) for q in paginated_items],
        counts_by_difficulty=counts_by_difficulty,
        counts_by_type=counts_by_type,
        counts_by_status=counts_by_status,
    )


def get_bank_question(
    db: Session,
    class_id: uuid.UUID,
    question_id: uuid.UUID,
) -> BankQuestion | None:
    """Retrieve a single bank question with relationships loaded."""
    stmt = (
        select(BankQuestion)
        .where(BankQuestion.id == question_id, BankQuestion.class_id == class_id)
        .options(
            selectinload(BankQuestion.options),
            selectinload(BankQuestion.skill),
            selectinload(BankQuestion.source_document),
        )
    )
    return db.scalar(stmt)


def create_bank_question(
    db: Session,
    class_id: uuid.UUID,
    payload: BankQuestionCreate,
    user_id: uuid.UUID | None = None,
) -> BankQuestion:
    """Persist a new question in the Question Bank."""
    grading_cfg = payload.grading_config or {}
    if payload.question_type == "FILL_IN_BLANK" and payload.correct_answers:
        grading_cfg[FILL_IN_BLANK_ANSWERS_KEY] = payload.correct_answers

    question = BankQuestion(
        id=uuid.uuid4(),
        class_id=class_id,
        skill_id=payload.skill_id,
        created_by_id=user_id,
        question_type=payload.question_type,
        question=payload.question.strip(),
        points=payload.points,
        difficulty=payload.difficulty,
        explanation=payload.explanation.strip() if payload.explanation else None,
        rubric=payload.rubric.strip() if payload.rubric else None,
        source_type=payload.source_type,
        source_document_id=payload.source_document_id,
        source_reference=payload.source_reference.strip() if payload.source_reference else None,
        status=payload.status,
        grading_config=grading_cfg if grading_cfg else None,
    )
    db.add(question)
    db.flush()

    if payload.question_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
        for idx, opt in enumerate(payload.options):
            option = BankQuestionOption(
                id=uuid.uuid4(),
                question_id=question.id,
                option_label=opt.option_label.upper(),
                option_text=opt.option_text.strip(),
                is_correct=opt.is_correct,
                order_index=opt.order_index if opt.order_index is not None else idx,
            )
            db.add(option)

    db.commit()
    db.refresh(question)
    return question


def update_bank_question(
    db: Session,
    class_id: uuid.UUID,
    question_id: uuid.UUID,
    payload: BankQuestionUpdate,
) -> BankQuestion:
    """Update fields and options of an existing bank question."""
    question = get_bank_question(db, class_id, question_id)
    if not question:
        raise ValueError("Bank question not found.")

    if payload.question_type is not None:
        question.question_type = payload.question_type
    if payload.question is not None:
        question.question = payload.question.strip()
    if payload.points is not None:
        question.points = payload.points
    if payload.difficulty is not None:
        question.difficulty = payload.difficulty
    if payload.skill_id is not None:
        question.skill_id = payload.skill_id
    if payload.explanation is not None:
        question.explanation = payload.explanation.strip() if payload.explanation else None
    if payload.rubric is not None:
        question.rubric = payload.rubric.strip() if payload.rubric else None
    if payload.source_type is not None:
        question.source_type = payload.source_type
    if payload.source_document_id is not None:
        question.source_document_id = payload.source_document_id
    if payload.source_reference is not None:
        question.source_reference = payload.source_reference.strip() if payload.source_reference else None
    if payload.status is not None:
        question.status = payload.status

    if payload.correct_answers is not None:
        cfg = question.grading_config or {}
        cfg[FILL_IN_BLANK_ANSWERS_KEY] = payload.correct_answers
        question.grading_config = cfg
    elif payload.grading_config is not None:
        question.grading_config = payload.grading_config

    if payload.options is not None:
        # Replace options
        for old_opt in list(question.options):
            db.delete(old_opt)
        db.flush()

        if question.question_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            for idx, opt in enumerate(payload.options):
                option = BankQuestionOption(
                    id=uuid.uuid4(),
                    question_id=question.id,
                    option_label=opt.option_label.upper(),
                    option_text=opt.option_text.strip(),
                    is_correct=opt.is_correct,
                    order_index=opt.order_index if opt.order_index is not None else idx,
                )
                db.add(option)

    db.commit()
    db.refresh(question)
    return question


def delete_bank_question(db: Session, class_id: uuid.UUID, question_id: uuid.UUID) -> bool:
    """Delete a question from the repository."""
    question = get_bank_question(db, class_id, question_id)
    if not question:
        return False
    db.delete(question)
    db.commit()
    return True


def bulk_update_status(
    db: Session,
    class_id: uuid.UUID,
    question_ids: list[uuid.UUID],
    status: str,
) -> int:
    """Update status (DRAFT, APPROVED, ARCHIVED) for multiple questions."""
    if status not in BANK_STATUSES:
        raise ValueError(f"Invalid status: {status}")

    stmt = select(BankQuestion).where(
        BankQuestion.class_id == class_id,
        BankQuestion.id.in_(question_ids),
    )
    questions = list(db.scalars(stmt).all())
    for q in questions:
        q.status = status
    db.commit()
    return len(questions)


def generate_questions_from_documents(
    db: Session,
    class_id: uuid.UUID,
    req: DocumentQuestionGenRequest,
    user_id: uuid.UUID | None = None,
) -> list[BankQuestion]:
    """Generate diagnostic questions from class documents and link to skill rubrics."""
    clazz = db.scalar(select(Class).where(Class.id == class_id))
    if not clazz:
        raise ValueError("Class not found.")

    # Fetch skills
    skills_query = select(Skill).where(Skill.class_id == class_id).options(selectinload(Skill.rubrics))
    if req.skill_ids:
        skills_query = skills_query.where(Skill.id.in_(req.skill_ids))
    available_skills = list(db.scalars(skills_query).all())

    # Build skill context string
    skill_code_to_id: dict[str, uuid.UUID] = {}
    skills_lines: list[str] = []
    for s in available_skills:
        skill_code_to_id[s.code.upper()] = s.id
        rubric_bullets = [f"      * [{r.level}]: {r.description}" for r in (s.rubrics or [])]
        skills_lines.append(
            f"- Code: {s.code}\n  Name: {s.name}\n  Category: {s.category}\n  Rubrics:\n"
            + "\n".join(rubric_bullets)
        )
    skills_context = "\n\n".join(skills_lines) if skills_lines else "(No class skills defined yet; generate general technical competencies)"

    # Fetch document contexts
    docs_query = select(Document).where(Document.class_id == class_id)
    if req.document_ids:
        docs_query = docs_query.where(Document.id.in_(req.document_ids))
    docs = list(db.scalars(docs_query).all())

    doc_snippets: list[str] = []
    primary_doc_id: uuid.UUID | None = None
    if docs:
        primary_doc_id = docs[0].id
        for doc in docs[:5]:
            doc_snippets.append(f"Document '{doc.file_name}':\n[Type: {doc.file_type}, Status: {doc.status}]")
    else:
        doc_snippets.append(f"Course syllabus & core modules for: {clazz.name} ({clazz.code})")

    document_content = "\n\n".join(doc_snippets)

    # Build difficulty breakdown per level
    if req.questions_per_level:
        active_levels = {k.upper(): v for k, v in req.questions_per_level.items() if v > 0}
        if not active_levels:
            active_levels = {"NOVICE": 2, "BEGINNER": 2, "COMPETENT": 1}
        num_questions = sum(active_levels.values())
        breakdown_lines = [
            f"- Level '{lvl}': generate EXACTLY {cnt} question(s)"
            for lvl, cnt in active_levels.items()
        ]
        difficulty_breakdown = "\n".join(breakdown_lines)
    else:
        num_questions = req.num_questions or 5
        levels = req.difficulty_levels or ["NOVICE", "BEGINNER", "COMPETENT"]
        difficulty_breakdown = f"Distribute {num_questions} questions evenly across levels: {', '.join(levels)}"

    # Load prompt
    prompt_mgr = get_prompt_manager()
    prompt_data = prompt_mgr.get("question-bank-gen-prompt")
    system_prompt = prompt_data.get("system-prompt", "")
    user_template = prompt_data.get("user-prompt", "")

    user_prompt = user_template.format(
        class_name=clazz.name,
        class_code=clazz.code,
        skills_context=skills_context,
        difficulty_breakdown=difficulty_breakdown,
        question_types=", ".join(req.question_types),
        num_questions=num_questions,
        document_content=document_content,
    )

    llm = get_llm_model()
    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ])

    raw_content = response.content if hasattr(response, "content") else str(response)
    if isinstance(raw_content, list):
        raw_content = "".join([part.get("text", "") if isinstance(part, dict) else str(part) for part in raw_content])

    json_match = re.search(r"\{[\s\S]*\}", raw_content)
    if not json_match:
        raise ValueError(f"LLM failed to return valid JSON output: {raw_content[:200]}")

    data = json.loads(json_match.group(0))
    questions_data = data.get("questions", [])

    created_questions: list[BankQuestion] = []
    for item in questions_data:
        q_type = str(item.get("question_type", "SINGLE_CHOICE")).upper()
        if q_type not in ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "FILL_IN_BLANK"):
            q_type = "SINGLE_CHOICE"

        skill_code = str(item.get("skill_code", "")).upper()
        matched_skill_id = skill_code_to_id.get(skill_code)
        if not matched_skill_id and available_skills:
            matched_skill_id = available_skills[0].id

        difficulty = str(item.get("difficulty", "NOVICE")).upper()

        grading_cfg: dict[str, Any] = {}
        fill_answers = item.get("correct_answers", [])
        if q_type == "FILL_IN_BLANK" and fill_answers:
            grading_cfg[FILL_IN_BLANK_ANSWERS_KEY] = fill_answers

        bq = BankQuestion(
            id=uuid.uuid4(),
            class_id=class_id,
            skill_id=matched_skill_id,
            created_by_id=user_id,
            question_type=q_type,
            question=item.get("question", "").strip(),
            points=float(item.get("points", 1.0)),
            difficulty=difficulty,
            explanation=item.get("explanation"),
            rubric=item.get("rubric"),
            source_type="DOCUMENT",
            source_document_id=primary_doc_id,
            source_reference=item.get("source_reference", f"Document extraction: {clazz.name}"),
            status="APPROVED",
            grading_config=grading_cfg if grading_cfg else None,
        )
        db.add(bq)
        db.flush()

        if q_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            raw_options = item.get("options", [])
            for opt_idx, opt in enumerate(raw_options):
                lbl = str(opt.get("option_label", chr(65 + opt_idx))).upper()
                opt_row = BankQuestionOption(
                    id=uuid.uuid4(),
                    question_id=bq.id,
                    option_label=lbl,
                    option_text=opt.get("option_text", "").strip(),
                    is_correct=bool(opt.get("is_correct", False)),
                    order_index=opt_idx,
                )
                db.add(opt_row)

        created_questions.append(bq)

    db.commit()
    return created_questions


def mine_misconceptions(
    db: Session,
    class_id: uuid.UUID,
    req: MisconceptionGenRequest,
    user_id: uuid.UUID | None = None,
) -> list[BankQuestion]:
    """Mine student chat logs & tickets for misunderstandings and generate diagnostic probe questions."""
    clazz = db.scalar(select(Class).where(Class.id == class_id))
    if not clazz:
        raise ValueError("Class not found.")

    # Fetch skills
    skills_query = select(Skill).where(Skill.class_id == class_id).options(selectinload(Skill.rubrics))
    if req.skill_ids:
        skills_query = skills_query.where(Skill.id.in_(req.skill_ids))
    available_skills = list(db.scalars(skills_query).all())

    skill_code_to_id: dict[str, uuid.UUID] = {}
    skills_lines: list[str] = []
    for s in available_skills:
        skill_code_to_id[s.code.upper()] = s.id
        rubric_bullets = [f"      * [{r.level}]: {r.description}" for r in (s.rubrics or [])]
        skills_lines.append(
            f"- Code: {s.code}\n  Name: {s.name}\n  Category: {s.category}\n  Rubrics:\n"
            + "\n".join(rubric_bullets)
        )
    skills_context = "\n\n".join(skills_lines) if skills_lines else "(General class skills)"

    # Query recent chat inquiries
    chat_stmt = (
        select(ChatMessage.content)
        .join(ChatSession, ChatMessage.session_id == ChatSession.id)
        .where(
            ChatSession.class_id == class_id,
            ChatMessage.role == "USER",
        )
        .order_by(ChatMessage.created_at.desc())
        .limit(20)
    )
    user_questions = list(db.scalars(chat_stmt).all())

    # Query recent tickets
    ticket_stmt = (
        select(Ticket.question, Ticket.agent_answer)
        .where(Ticket.class_id == class_id)
        .order_by(Ticket.created_at.desc())
        .limit(10)
    )
    tickets = list(db.execute(ticket_stmt).all())

    inquiry_snippets: list[str] = []
    for q_text in user_questions:
        inquiry_snippets.append(f"Student chat question: {q_text}")
    for t_q, t_ans in tickets:
        inquiry_snippets.append(f"Ticket question: {t_q}")
        if t_ans:
            inquiry_snippets.append(f"Agent reply context: {t_ans[:200]}")

    if not inquiry_snippets:
        inquiry_snippets.append(
            f"Common student misconceptions and friction points in {clazz.name} ({clazz.code}): "
            "pointers vs references, shallow vs deep copying, memory leaks with dynamic allocation, "
            "asynchronous callback timing, and off-by-one boundary handling."
        )

    student_inquiries = "\n".join(inquiry_snippets[:15])

    # Build difficulty breakdown per level
    if req.questions_per_level:
        active_levels = {k.upper(): v for k, v in req.questions_per_level.items() if v > 0}
        if not active_levels:
            active_levels = {"BEGINNER": 2, "COMPETENT": 2}
        num_questions = sum(active_levels.values())
        breakdown_lines = [
            f"- Level '{lvl}': generate EXACTLY {cnt} probe question(s)"
            for lvl, cnt in active_levels.items()
        ]
        difficulty_breakdown = "\n".join(breakdown_lines)
    else:
        num_questions = req.num_questions or 5
        difficulty_breakdown = f"Synthesize {num_questions} diagnostic questions across relevant Dreyfus levels (NOVICE to MASTER)."

    prompt_mgr = get_prompt_manager()
    prompt_data = prompt_mgr.get("misconception-probing-prompt")
    system_prompt = prompt_data.get("system-prompt", "")
    user_template = prompt_data.get("user-prompt", "")

    user_prompt = user_template.format(
        class_name=clazz.name,
        class_code=clazz.code,
        skills_context=skills_context,
        difficulty_breakdown=difficulty_breakdown,
        student_inquiries=student_inquiries,
        num_questions=num_questions,
    )

    llm = get_llm_model()
    response = llm.invoke([
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ])

    raw_content = response.content if hasattr(response, "content") else str(response)
    if isinstance(raw_content, list):
        raw_content = "".join([part.get("text", "") if isinstance(part, dict) else str(part) for part in raw_content])

    json_match = re.search(r"\{[\s\S]*\}", raw_content)
    if not json_match:
        raise ValueError(f"LLM failed to return valid JSON output: {raw_content[:200]}")

    data = json.loads(json_match.group(0))
    questions_data = data.get("questions", [])

    created_questions: list[BankQuestion] = []
    for item in questions_data:
        q_type = str(item.get("question_type", "SINGLE_CHOICE")).upper()
        if q_type not in ("SINGLE_CHOICE", "MULTIPLE_CHOICE", "FILL_IN_BLANK"):
            q_type = "SINGLE_CHOICE"

        skill_code = str(item.get("skill_code", "")).upper()
        matched_skill_id = skill_code_to_id.get(skill_code)
        if not matched_skill_id and available_skills:
            matched_skill_id = available_skills[0].id

        difficulty = str(item.get("difficulty", "COMPETENT")).upper()

        grading_cfg: dict[str, Any] = {}
        fill_answers = item.get("correct_answers", [])
        if q_type == "FILL_IN_BLANK" and fill_answers:
            grading_cfg[FILL_IN_BLANK_ANSWERS_KEY] = fill_answers

        bq = BankQuestion(
            id=uuid.uuid4(),
            class_id=class_id,
            skill_id=matched_skill_id,
            created_by_id=user_id,
            question_type=q_type,
            question=item.get("question", "").strip(),
            points=float(item.get("points", 1.0)),
            difficulty=difficulty,
            explanation=item.get("explanation"),
            rubric=item.get("rubric"),
            source_type="CHAT_MISCONCEPTION",
            source_reference=item.get("source_reference", "Mined from student inquiries"),
            status="APPROVED",
            grading_config=grading_cfg if grading_cfg else None,
        )
        db.add(bq)
        db.flush()

        if q_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            raw_options = item.get("options", [])
            for opt_idx, opt in enumerate(raw_options):
                lbl = str(opt.get("option_label", chr(65 + opt_idx))).upper()
                opt_row = BankQuestionOption(
                    id=uuid.uuid4(),
                    question_id=bq.id,
                    option_label=lbl,
                    option_text=opt.get("option_text", "").strip(),
                    is_correct=bool(opt.get("is_correct", False)),
                    order_index=opt_idx,
                )
                db.add(opt_row)

        created_questions.append(bq)

    db.commit()
    return created_questions


def export_bank_to_excel(
    db: Session,
    class_id: uuid.UUID,
    skill_id: uuid.UUID | None = None,
    question_type: str | None = None,
    difficulty: str | None = None,
    status: str | None = None,
) -> io.BytesIO:
    """Export filtered bank questions to an in-memory .xlsx file matching the 14-column contract."""
    query = (
        select(BankQuestion)
        .where(BankQuestion.class_id == class_id)
        .options(
            selectinload(BankQuestion.options),
            selectinload(BankQuestion.skill),
        )
        .order_by(BankQuestion.created_at.asc())
    )
    if skill_id:
        query = query.where(BankQuestion.skill_id == skill_id)
    if question_type:
        query = query.where(BankQuestion.question_type == question_type.upper())
    if difficulty:
        query = query.where(BankQuestion.difficulty == difficulty.upper())
    if status and status.upper() != "ALL":
        query = query.where(BankQuestion.status == status.upper())

    questions = list(db.scalars(query).all())
    wb = export_quiz_to_workbook(questions)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output


def import_bank_from_excel(
    db: Session,
    class_id: uuid.UUID,
    file_bytes: bytes,
    user_id: uuid.UUID | None = None,
) -> tuple[int, list[str]]:
    """Import questions from an Excel file conforming to the 14-column contract into the repository."""
    parse_result = validate_and_parse_quiz_excel(file_bytes)
    if not parse_result.is_valid:
        error_msgs = [f"Row {err.row_index}: {err.message}" for err in parse_result.errors]
        raise ValueError("; ".join(error_msgs[:5]))

    # Default skill if available
    default_skill = db.scalar(
        select(Skill).where(Skill.class_id == class_id).order_by(Skill.created_at.asc())
    )

    imported_count = 0
    for q_dto in parse_result.questions:
        grading_cfg: dict[str, Any] = {}
        if q_dto.question_type == "FILL_IN_BLANK" and q_dto.correct_answers:
            grading_cfg[FILL_IN_BLANK_ANSWERS_KEY] = q_dto.correct_answers

        bq = BankQuestion(
            id=uuid.uuid4(),
            class_id=class_id,
            skill_id=default_skill.id if default_skill else None,
            created_by_id=user_id,
            question_type=q_dto.question_type,
            question=q_dto.question.strip(),
            points=q_dto.points,
            difficulty=q_dto.difficulty,
            explanation=q_dto.explanation,
            rubric=q_dto.rubric,
            source_type="EXCEL_IMPORT",
            source_reference=q_dto.source_reference,
            status="APPROVED",
            grading_config=grading_cfg if grading_cfg else None,
        )
        db.add(bq)
        db.flush()

        if q_dto.question_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            for opt_idx, opt in enumerate(q_dto.options):
                opt_row = BankQuestionOption(
                    id=uuid.uuid4(),
                    question_id=bq.id,
                    option_label=opt.option_label.upper(),
                    option_text=opt.option_text.strip(),
                    is_correct=opt.is_correct,
                    order_index=opt_idx,
                )
                db.add(opt_row)

        imported_count += 1

    db.commit()
    warning_msgs = [f"Row {w.row_index}: {w.message}" for w in parse_result.warnings]
    return imported_count, warning_msgs
