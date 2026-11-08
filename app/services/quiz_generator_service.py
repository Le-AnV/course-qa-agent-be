"""Blueprint sampling and intelligent quiz generation service (AI-149 / AI-151).

Implements standardized blueprint matrix sampling from Question Bank with
on-demand document generation fallback and per-question regeneration.
"""

from __future__ import annotations

import logging
import random
import uuid
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


def generate_quiz_from_blueprint(
    db: Session,
    class_id: uuid.UUID,
    req: QuizGenerateBlueprintRequest,
    user_id: uuid.UUID,
) -> Quiz:
    """Sample questions from Question Bank using a standardized blueprint matrix.

    Triggers on-demand document generation if Bank items for any cell are insufficient
    and auto_generate_missing is enabled.
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

    chosen_questions: list[BankQuestion] = []
    chosen_ids: set[uuid.UUID] = set()

    # 2. Sample or auto-generate questions for each cell
    for cell in cells_to_sample:
        needed = cell.count
        diff_str = str(cell.difficulty).upper()

        # Query available questions from Bank
        bank_query = (
            select(BankQuestion)
            .options(selectinload(BankQuestion.options))
            .where(
                BankQuestion.class_id == class_id,
                BankQuestion.skill_id == cell.skill_id,
                func.upper(BankQuestion.difficulty) == diff_str,
            )
        )
        available = [q for q in db.scalars(bank_query).all() if q.id not in chosen_ids]
        random.shuffle(available)

        picked = available[:needed]
        for q in picked:
            chosen_questions.append(q)
            chosen_ids.add(q.id)

        missing = needed - len(picked)
        if missing > 0:
            if req.auto_generate_missing:
                logger.info(
                    "Cell (skill=%s, diff=%s) lacks %d questions; triggering on-demand generation.",
                    cell.skill_id,
                    diff_str,
                    missing,
                )
                try:
                    generated = question_bank_service.generate_questions_from_documents(
                        db=db,
                        class_id=class_id,
                        req=DocumentQuestionGenRequest(
                            document_ids=[str(d) for d in req.document_ids],
                            skill_ids=[str(cell.skill_id)],
                            question_types=["SINGLE_CHOICE"],
                            difficulty_levels=[cell.difficulty],
                            questions_per_level={diff_str: missing},
                            num_questions=missing,
                        ),
                        user_id=user_id,
                    )
                    for gq in generated:
                        chosen_questions.append(gq)
                        chosen_ids.add(gq.id)
                except Exception as exc:
                    logger.warning("On-demand fallback generation failed: %s", exc)
                    # If generator failed, keep what we have or raise if no questions picked
                    if not picked:
                        raise ValueError(
                            f"Could not sample or generate questions for skill {cell.skill_id} at {diff_str}."
                        ) from exc
            else:
                raise ValueError(
                    f"Insufficient questions in Question Bank for skill {cell.skill_id} at {diff_str} "
                    f"(required {needed}, available {len(picked)})."
                )

    if not chosen_questions:
        raise ValueError("Could not assemble any questions for the requested blueprint.")

    # 3. Apply question-level shuffle if requested
    if req.shuffle_questions:
        random.shuffle(chosen_questions)

    # 4. Create Quiz entity
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
    db.add(quiz)
    db.flush()

    # 5. Populate QuizQuestion and QuizOption records
    labels = ["A", "B", "C", "D", "E", "F"]
    for idx, bq in enumerate(chosen_questions):
        qq = QuizQuestion(
            quiz_id=quiz.id,
            skill_id=bq.skill_id,
            question_type=bq.question_type or "SINGLE_CHOICE",
            question=bq.question,
            points=bq.points if bq.points > 0 else 1.0,
            difficulty=bq.difficulty or "NOVICE",
            order_index=idx,
            explanation=bq.explanation,
            rubric=bq.rubric,
            source_reference=bq.source_reference,
            grading_config=bq.grading_config,
        )
        db.add(qq)
        db.flush()

        raw_options = list(bq.options or [])
        if req.shuffle_options and bq.question_type in ("SINGLE_CHOICE", "MULTIPLE_CHOICE"):
            random.shuffle(raw_options)

        for opt_idx, opt in enumerate(raw_options):
            label = labels[opt_idx] if opt_idx < len(labels) else f"Opt{opt_idx + 1}"
            qo = QuizOption(
                question_id=qq.id,
                option_label=label,
                option_text=opt.option_text,
                is_correct=opt.is_correct,
                order_index=opt_idx,
            )
            db.add(qo)

    db.commit()
    db.refresh(quiz)
    return quiz


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
