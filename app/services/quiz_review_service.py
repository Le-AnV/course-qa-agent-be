"""Teacher review, approval, and Excel export of a quiz (AI-151)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.quiz import Quiz, QuizOption, QuizQuestion
from app.models.user import User
from app.schemas.quiz import QuizQuestionCreate, QuizUpdate
from app.services.quiz_excel import FILL_IN_BLANK_ANSWERS_KEY

EDITABLE_STATUS = "DRAFT"
APPROVED_STATUS = "PUBLISHED"


class QuizStateError(ValueError):
    """The quiz is not in a state that allows the requested operation."""


def get_quiz(db: Session, class_id: uuid.UUID, quiz_id: uuid.UUID) -> Quiz | None:
    """Fetch a quiz only when it belongs to ``class_id``."""
    quiz = db.get(Quiz, quiz_id)
    if quiz is None or quiz.class_id != class_id:
        return None
    return quiz


def get_question(quiz: Quiz, question_id: uuid.UUID) -> QuizQuestion | None:
    for question in quiz.questions:
        if question.id == question_id:
            return question
    return None


def _require_draft(quiz: Quiz) -> None:
    if quiz.status != EDITABLE_STATUS:
        raise QuizStateError("Only a draft quiz can be edited.")


def _apply_question_payload(
    db: Session, question: QuizQuestion, data: QuizQuestionCreate
) -> None:
    """Copy a validated payload onto an existing question, replacing its options.

    Old options are deleted and flushed first: ``uq_quiz_question_option_label``
    would otherwise collide while the replacement rows are inserted.
    """
    question.question_type = data.question_type
    question.question = data.question
    question.points = data.points
    question.difficulty = data.difficulty
    question.explanation = data.explanation
    question.rubric = data.rubric
    question.source_reference = data.source_reference

    grading_config = dict(data.grading_config or {})
    if data.question_type == "FILL_IN_BLANK":
        grading_config[FILL_IN_BLANK_ANSWERS_KEY] = list(data.correct_answers)
    question.grading_config = grading_config or None

    if question.options:
        question.options.clear()
        db.flush()

    question.options = [
        QuizOption(
            option_label=option.option_label,
            option_text=option.option_text,
            is_correct=option.is_correct,
            order_index=index,
        )
        for index, option in enumerate(data.options)
    ]


def _new_question(
    db: Session, quiz_id: uuid.UUID, data: QuizQuestionCreate, order_index: int
) -> QuizQuestion:
    question = QuizQuestion(quiz_id=quiz_id, order_index=order_index)
    _apply_question_payload(db, question, data)
    return question


def _resequence(quiz: Quiz) -> None:
    for index, question in enumerate(sorted(quiz.questions, key=lambda q: q.order_index)):
        question.order_index = index


def update_metadata(db: Session, quiz: Quiz, payload: QuizUpdate) -> Quiz:
    _require_draft(quiz)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(quiz, field, value)
    db.commit()
    db.refresh(quiz)
    return quiz


def add_question(db: Session, quiz: Quiz, data: QuizQuestionCreate) -> Quiz:
    _require_draft(quiz)
    next_index = max((q.order_index for q in quiz.questions), default=-1) + 1
    quiz.questions.append(_new_question(db, quiz.id, data, next_index))
    db.commit()
    db.refresh(quiz)
    return quiz


def replace_question(
    db: Session, quiz: Quiz, question: QuizQuestion, data: QuizQuestionCreate
) -> Quiz:
    _require_draft(quiz)
    _apply_question_payload(db, question, data)
    db.commit()
    db.refresh(quiz)
    return quiz


def delete_question(db: Session, quiz: Quiz, question: QuizQuestion) -> Quiz:
    _require_draft(quiz)
    quiz.questions.remove(question)
    db.flush()
    _resequence(quiz)
    db.commit()
    db.refresh(quiz)
    return quiz


def approve(db: Session, quiz: Quiz, approver: User) -> Quiz:
    if quiz.status != EDITABLE_STATUS:
        raise QuizStateError("Only a draft quiz can be approved.")
    if not quiz.questions:
        raise QuizStateError("Only a draft quiz with at least one question can be approved.")
    quiz.status = APPROVED_STATUS
    quiz.approved_by_id = approver.id
    quiz.approved_at = datetime.now(UTC)
    db.commit()
    db.refresh(quiz)
    return quiz


def reopen(db: Session, quiz: Quiz) -> Quiz:
    if quiz.status != APPROVED_STATUS:
        raise QuizStateError("Only an approved quiz can be reopened.")
    quiz.status = EDITABLE_STATUS
    quiz.approved_by_id = None
    quiz.approved_at = None
    db.commit()
    db.refresh(quiz)
    return quiz


def delete_quiz(db: Session, quiz: Quiz) -> None:
    if quiz.status != EDITABLE_STATUS:
        raise QuizStateError("Only a draft quiz can be deleted.")
    db.delete(quiz)
    db.commit()


def require_exportable(quiz: Quiz) -> None:
    if quiz.status != APPROVED_STATUS:
        raise QuizStateError("Approve this quiz before exporting it.")
