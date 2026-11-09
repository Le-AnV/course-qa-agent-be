"""Blueprint sampling and intelligent quiz generation service (AI-149 / AI-151).

Implements standardized blueprint matrix sampling from Question Bank with
on-demand document generation fallback and per-question regeneration.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models.course import Class
from app.models.question_bank import BankQuestion
from app.models.quiz import BLOOM_LEVELS, Quiz, QuizOption, QuizQuestion
from app.models.skill import Skill
from app.models.user import User
from app.schemas.question_bank import DocumentQuestionGenRequest
from app.schemas.quiz import (
    BlueprintAvailabilityResponse,
    BlueprintCellAvailability,
    BlueprintSkillCell,
    QuizGenerateBlueprintRequest,
    QuizOptionCreate,
    QuizQuestionCreate,
    SkillMasteryLevelBreakdown,
    SkillMasteryRadarItem,
)
from app.services import question_bank_service, quiz_generation_service
from app.services.quiz_excel import FILL_IN_BLANK_ANSWERS_KEY
from app.services.quiz_validation import (
    MAX_REGENERATION_ATTEMPTS,
    GeneratedQuestion,
    ValidationSpec,
    validate_batch,
)
from app.services.skill_taxonomy_service import evaluate_skill_taxonomy

logger = logging.getLogger(__name__)


def get_blueprint_availability(db: Session, class_id: uuid.UUID) -> BlueprintAvailabilityResponse:
    """Return Question Bank question counts mapped across class skills and Dreyfus tiers."""
    clazz = db.scalar(select(Class).where(Class.id == class_id))
    if not clazz:
        raise ValueError("Class not found.")

    skills = list(
        db.scalars(
            select(Skill)
            .where(Skill.class_id == class_id)
            .order_by(Skill.code.asc())
        ).all()
    )

    # Count bank questions grouped by (skill_id, difficulty)
    counts_query = (
        select(
            BankQuestion.skill_id,
            BankQuestion.difficulty,
            func.count(BankQuestion.id).label("count"),
        )
        .where(
            BankQuestion.class_id == class_id,
            BankQuestion.skill_id.is_not(None),
        )
        .group_by(BankQuestion.skill_id, BankQuestion.difficulty)
    )
    counts_rows = db.execute(counts_query).all()
    counts_map: dict[tuple[uuid.UUID, str], int] = {
        (row[0], row[1].upper()): row[2] for row in counts_rows
    }

    cells: list[BlueprintCellAvailability] = []
    total_bank = 0
    levels = list(BLOOM_LEVELS)

    for skill in skills:
        for lvl in levels:
            c = counts_map.get((skill.id, lvl), 0)
            total_bank += c
            cells.append(
                BlueprintCellAvailability(
                    skill_id=skill.id,
                    skill_code=skill.code,
                    skill_name=skill.name,
                    difficulty=lvl,
                    available_in_bank=c,
                )
            )

    return BlueprintAvailabilityResponse(
        class_id=class_id,
        cells=cells,
        total_bank_questions=total_bank,
    )


@dataclass
class _ValidatedQuestion:
    """A validated question payload bound to the blueprint cell it fills."""

    payload: QuizQuestionCreate
    skill_id: uuid.UUID
    difficulty: str
    confidence: float | None = None
    flags: list[str] = field(default_factory=list)


def _bank_question_payload(bank_question: Any) -> dict[str, Any]:
    """Build a validator payload for a bank/generated row, relabelling options A-F."""

    question_type = str(
        getattr(bank_question, "question_type", "") or "SINGLE_CHOICE"
    ).upper()
    labels = ["A", "B", "C", "D", "E", "F"]
    options: list[dict[str, Any]] = []
    if question_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
        for index, option in enumerate(list(getattr(bank_question, "options", None) or [])):
            label = labels[index] if index < len(labels) else f"Opt{index + 1}"
            options.append(
                {
                    "option_label": label,
                    "option_text": getattr(option, "option_text", ""),
                    "is_correct": bool(getattr(option, "is_correct", False)),
                }
            )

    grading_config = getattr(bank_question, "grading_config", None)
    correct_answers: list[str] = []
    if question_type == "FILL_IN_BLANK" and isinstance(grading_config, dict):
        raw_answers = grading_config.get(FILL_IN_BLANK_ANSWERS_KEY)
        if isinstance(raw_answers, list):
            correct_answers = [str(answer) for answer in raw_answers]
        elif isinstance(raw_answers, str):
            correct_answers = [raw_answers]

    points = getattr(bank_question, "points", None)
    if not isinstance(points, (int, float)) or points <= 0:
        points = 1.0

    return {
        "question_type": question_type,
        "question": getattr(bank_question, "question", "") or "",
        "points": points,
        "difficulty": str(getattr(bank_question, "difficulty", "") or "NOVICE").upper(),
        "explanation": getattr(bank_question, "explanation", None),
        "rubric": getattr(bank_question, "rubric", None),
        "source_reference": getattr(bank_question, "source_reference", None),
        "grading_config": grading_config,
        "options": options,
        "correct_answers": correct_answers,
    }


def _candidate_from_bank(bank_question: Any) -> tuple[GeneratedQuestion, list[str]]:
    """Wrap a bank/generated row as a validator candidate plus its source flags."""

    source_flags = getattr(bank_question, "validation_flags", None) or []
    return (
        GeneratedQuestion(
            data=_bank_question_payload(bank_question),
            confidence=getattr(bank_question, "confidence", None),
        ),
        [str(flag) for flag in source_flags],
    )


def _matches_cell(bank_question: Any, *, class_id: uuid.UUID, cell: BlueprintSkillCell) -> bool:
    """Return whether a generated row belongs to the requested blueprint cell."""

    if getattr(bank_question, "class_id", None) != class_id:
        return False
    if getattr(bank_question, "skill_id", None) != cell.skill_id:
        return False
    difficulty = str(getattr(bank_question, "difficulty", "") or "").upper()
    return difficulty == str(cell.difficulty).upper()


def _collect_cell_questions(
    db: Session,
    *,
    class_id: uuid.UUID,
    cell: BlueprintSkillCell,
    available: Sequence[BankQuestion],
    accepted: Sequence[_ValidatedQuestion],
    req: QuizGenerateBlueprintRequest,
    user_id: uuid.UUID,
) -> list[_ValidatedQuestion]:
    """Validate the bank pool for one cell and refill missing slots with bounded generation."""

    needed = cell.count
    difficulty = str(cell.difficulty).upper()
    cell_questions: list[_ValidatedQuestion] = []

    def validate(candidates: list[tuple[GeneratedQuestion, list[str]]], remaining: int) -> None:
        if not candidates:
            return
        spec = ValidationSpec(total_questions=remaining)
        existing = [item.payload for item in accepted] + [
            item.payload for item in cell_questions
        ]
        result = validate_batch(
            [candidate for candidate, _ in candidates],
            spec,
            existing=existing,
        )
        for outcome, (_, source_flags) in zip(result.results, candidates, strict=True):
            if not outcome.accepted or outcome.question is None:
                continue
            flags = list(dict.fromkeys([*source_flags, *outcome.flags]))
            cell_questions.append(
                _ValidatedQuestion(
                    payload=outcome.question,
                    skill_id=cell.skill_id,
                    difficulty=difficulty,
                    confidence=outcome.confidence,
                    flags=flags,
                )
            )

    validate([_candidate_from_bank(bank_question) for bank_question in available], needed)

    attempts = 0
    while (
        req.auto_generate_missing
        and len(cell_questions) < needed
        and attempts < MAX_REGENERATION_ATTEMPTS
    ):
        attempts += 1
        missing = needed - len(cell_questions)
        logger.info(
            "Cell (skill=%s, diff=%s) still lacks %d valid questions; generation attempt %d/%d.",
            cell.skill_id,
            difficulty,
            missing,
            attempts,
            MAX_REGENERATION_ATTEMPTS,
        )
        try:
            generated = question_bank_service.generate_questions_from_documents(
                db=db,
                class_id=class_id,
                req=DocumentQuestionGenRequest(
                    document_ids=[str(document_id) for document_id in req.document_ids],
                    skill_ids=[str(cell.skill_id)],
                    question_types=["SINGLE_CHOICE"],
                    difficulty_levels=[cell.difficulty],
                    questions_per_level={difficulty: missing},
                    num_questions=missing,
                ),
                user_id=user_id,
            )
        except Exception as exc:  # noqa: BLE001 - generator boundary
            logger.warning("On-demand fallback generation failed: %s", exc)
            continue

        candidates = [
            _candidate_from_bank(bank_question)
            for bank_question in (generated or [])
            if _matches_cell(bank_question, class_id=class_id, cell=cell)
        ]
        validate(candidates, missing)

    if len(cell_questions) < needed:
        raise ValueError(
            f"Insufficient valid questions for skill {cell.skill_id} at {difficulty}: "
            f"required {needed}, validated {len(cell_questions)}."
        )
    return cell_questions


def _persist_blueprint_quiz(
    db: Session,
    *,
    class_id: uuid.UUID,
    req: QuizGenerateBlueprintRequest,
    user_id: uuid.UUID,
    questions: Sequence[_ValidatedQuestion],
) -> Quiz:
    """Create the quiz and its questions only after every cell passed validation."""

    labels = ["A", "B", "C", "D", "E", "F"]
    quiz = Quiz(
        class_id=class_id,
        creator_id=user_id,
        title=req.title,
        description=req.description,
        time_limit_minutes=req.time_limit_minutes,
        passing_score=req.passing_score,
        opens_at=req.opens_at,
        closes_at=req.closes_at,
        max_attempts=req.max_attempts,
        show_solutions_after_submission=req.show_solutions_after_submission,
        status="DRAFT",
    )

    try:
        db.add(quiz)
        db.flush()

        for index, draft in enumerate(questions):
            payload = draft.payload
            question = QuizQuestion(
                quiz_id=quiz.id,
                skill_id=draft.skill_id,
                question_type=payload.question_type,
                question=payload.question,
                points=payload.points,
                difficulty=draft.difficulty,
                order_index=index,
                explanation=payload.explanation,
                rubric=payload.rubric,
                source_reference=payload.source_reference,
                grading_config=dict(payload.grading_config) if payload.grading_config else None,
                confidence=draft.confidence,
                validation_flags=list(draft.flags) or None,
            )
            db.add(question)
            db.flush()

            options = list(payload.options)
            if req.shuffle_options and payload.question_type in (
                "SINGLE_CHOICE",
                "MULTIPLE_CHOICE",
            ):
                random.shuffle(options)

            for option_index, option in enumerate(options):
                label = (
                    labels[option_index]
                    if option_index < len(labels)
                    else f"Opt{option_index + 1}"
                )
                db.add(
                    QuizOption(
                        question_id=question.id,
                        option_label=label,
                        option_text=option.option_text,
                        is_correct=option.is_correct,
                        order_index=option_index,
                    )
                )

        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(quiz)
    return quiz


def generate_quiz_from_blueprint(
    db: Session,
    class_id: uuid.UUID,
    req: QuizGenerateBlueprintRequest,
    user_id: uuid.UUID,
) -> Quiz:
    """Sample and validate questions from the Question Bank using a blueprint matrix.

    Every candidate passes the shared AI-150 validator before the quiz is created:
    malformed bank rows, duplicate option text and exact/near duplicate questions
    (also across blueprint cells) never reach the draft. Missing slots are refilled
    from the same cell with bounded AI-149 document generation when enabled.
    """
    clazz = db.scalar(select(Class).where(Class.id == class_id))
    if not clazz:
        raise ValueError("Class not found.")

    # 1. Resolve requested blueprint distribution cells
    cells_to_sample: list[BlueprintSkillCell] = []
    if req.custom_distribution:
        cells_to_sample = list(req.custom_distribution)
    else:
        for skill_id in req.target_skill_ids:
            for lvl in req.target_levels:
                cells_to_sample.append(
                    BlueprintSkillCell(
                        skill_id=skill_id,
                        difficulty=lvl,
                        count=req.questions_per_level,
                    )
                )

    if not cells_to_sample:
        raise ValueError("No blueprint cells specified for quiz generation.")

    chosen_questions: list[_ValidatedQuestion] = []

    # 2. Validate each cell's bank pool and fill shortfalls from the same cell
    for cell in cells_to_sample:
        difficulty = str(cell.difficulty).upper()
        bank_query = (
            select(BankQuestion)
            .options(selectinload(BankQuestion.options))
            .where(
                BankQuestion.class_id == class_id,
                BankQuestion.skill_id == cell.skill_id,
                func.upper(BankQuestion.difficulty) == difficulty,
            )
        )
        available = list(db.scalars(bank_query).all())
        random.shuffle(available)
        chosen_questions.extend(
            _collect_cell_questions(
                db,
                class_id=class_id,
                cell=cell,
                available=available,
                accepted=chosen_questions,
                req=req,
                user_id=user_id,
            )
        )

    if not chosen_questions:
        raise ValueError("Could not assemble any questions for the requested blueprint.")

    # 3. Apply question-level shuffle if requested
    if req.shuffle_questions:
        random.shuffle(chosen_questions)

    # 4. Create the quiz only after validation has accepted every requested slot
    return _persist_blueprint_quiz(
        db,
        class_id=class_id,
        req=req,
        user_id=user_id,
        questions=chosen_questions,
    )


def swap_or_regenerate_question(
    *,
    db: Session,
    question: QuizQuestion,
    instructions: str | None = None,
    requested_by: User,
) -> QuizQuestionCreate:
    """Swap an individual quiz question with a matching alternative from Bank or generate fresh."""
    quiz = db.scalar(select(Quiz).where(Quiz.id == question.quiz_id))
    if not quiz:
        raise ValueError("Quiz not found.")

    class_id = quiz.class_id
    existing_q_texts = {
        q.question.strip().lower()
        for q in db.scalars(select(QuizQuestion).where(QuizQuestion.quiz_id == quiz.id)).all()
    }

    # Find another question in Question Bank with identical skill and difficulty
    bank_query = (
        select(BankQuestion)
        .options(selectinload(BankQuestion.options))
        .where(
            BankQuestion.class_id == class_id,
            func.upper(BankQuestion.difficulty) == question.difficulty.upper(),
        )
    )
    if question.skill_id:
        bank_query = bank_query.where(BankQuestion.skill_id == question.skill_id)

    candidates = [
        bq
        for bq in db.scalars(bank_query).all()
        if bq.question.strip().lower() not in existing_q_texts
    ]

    labels = ["A", "B", "C", "D", "E", "F"]
    if candidates:
        chosen = random.choice(candidates)
        options_create: list[QuizOptionCreate] = []
        for idx, opt in enumerate(chosen.options or []):
            label = labels[idx] if idx < len(labels) else f"Opt{idx + 1}"
            options_create.append(
                QuizOptionCreate(
                    option_label=label,
                    option_text=opt.option_text,
                    is_correct=opt.is_correct,
                )
            )
        return QuizQuestionCreate(
            question_type=chosen.question_type or question.question_type,
            question=chosen.question,
            points=question.points,
            difficulty=chosen.difficulty or question.difficulty,
            skill_id=chosen.skill_id or question.skill_id,
            explanation=chosen.explanation,
            rubric=chosen.rubric,
            source_reference=chosen.source_reference,
            options=options_create,
        )

    # Fallback: on-demand generate a single replacement question from class documents
    generated = question_bank_service.generate_questions_from_documents(
        db=db,
        class_id=class_id,
        req=DocumentQuestionGenRequest(
            document_ids=[],
            skill_ids=[str(question.skill_id)] if question.skill_id else [],
            question_types=[question.question_type],
            difficulty_levels=[question.difficulty],
            questions_per_level={question.difficulty: 1},
            num_questions=1,
        ),
        user_id=requested_by.id,
    )
    if not generated:
        raise ValueError("No replacement question could be found or generated.")

    fresh = generated[0]
    options_create: list[QuizOptionCreate] = []
    for idx, opt in enumerate(fresh.options or []):
        label = labels[idx] if idx < len(labels) else f"Opt{idx + 1}"
        options_create.append(
            QuizOptionCreate(
                option_label=label,
                option_text=opt.option_text,
                is_correct=opt.is_correct,
            )
        )

    return QuizQuestionCreate(
        question_type=fresh.question_type or question.question_type,
        question=fresh.question,
        points=question.points,
        difficulty=fresh.difficulty or question.difficulty,
        skill_id=fresh.skill_id or question.skill_id,
        explanation=fresh.explanation,
        rubric=fresh.rubric,
        source_reference=fresh.source_reference,
        options=options_create,
    )


def compute_skill_radar_report(
    quiz: Quiz,
    answers_by_qid: dict[uuid.UUID, Any],
) -> list[SkillMasteryRadarItem]:
    """Compute the multi-dimensional Dreyfus Skill Radar report for a quiz submission."""
    items_by_skill: dict[str, dict[str, Any]] = {}

    for q in quiz.questions:
        if not q.skill_id:
            continue
        code = q.skill.code if q.skill else str(q.skill_id)
        name = q.skill.name if q.skill else code
        if code not in items_by_skill:
            items_by_skill[code] = {
                "skill_id": q.skill_id,
                "skill_name": name,
                "answers": [],
            }
        ans = answers_by_qid.get(q.id)
        earned = Decimal(str(ans.score_awarded or 0)) if ans else Decimal(0)
        max_p = Decimal(str(q.points or 1.0))
        diff = q.difficulty.upper() if q.difficulty else "NOVICE"
        items_by_skill[code]["answers"].append(
            {
                "difficulty": diff,
                "earned_pts": earned,
                "max_pts": max_p,
            }
        )

    if not items_by_skill:
        return []

    evaluated = evaluate_skill_taxonomy(items_by_skill)
    skills_dict = evaluated.get("skills", {})
    radar_items: list[SkillMasteryRadarItem] = []

    for code, res in skills_dict.items():
        breakdown_dict = {}
        for lvl, b in res.get("level_breakdown", {}).items():
            breakdown_dict[lvl] = SkillMasteryLevelBreakdown(
                count=b.get("count", 0),
                earned=b.get("earned", 0.0),
                max=b.get("max", 0.0),
                rate=b.get("rate"),
                passed=b.get("passed", False),
                threshold=b.get("threshold", 70),
                status=b.get("status", "NO_DATA"),
            )
        gaps = []
        if res.get("has_foundation_gap"):
            gaps.append(
                f"Foundation gap detected: passed higher levels but blocked at {res.get('blocked_at')}."
            )
        radar_items.append(
            SkillMasteryRadarItem(
                skill_id=str(res.get("skill_id", "")),
                skill_code=code,
                skill_name=res.get("skill_name", code),
                achieved_level=res.get("achieved_level", "NONE"),
                ceiling_level=res.get("ceiling_level"),
                percentage=res.get("radar_value", 0.0),
                is_blocked=res.get("blocked_at") is not None,
                blocked_at=res.get("blocked_at"),
                reason=res.get("reason"),
                foundation_gaps=gaps,
                breakdown=breakdown_dict,
            )
        )

    return radar_items


# Install our swap_or_regenerate_question handler into quiz_generation_service
quiz_generation_service.set_regenerator(swap_or_regenerate_question)
