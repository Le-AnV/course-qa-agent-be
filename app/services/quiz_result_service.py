"""Student results, teacher submissions, statistics and Excel export (AI-154).

Grading itself lives in :mod:`app.services.quiz_attempt_service`. This module is
the read side: it turns persisted attempts into the student result/history and
the teacher submissions/statistics payloads, and builds the class results
workbook (Summary, Submissions, PerQuestion).

Lazy grading: AI-153 owns the submit endpoint and calls ``grade_attempt`` after
a successful submit. If a submitted attempt is read before that hook ran (or a
grading retry is needed), every read path here re-runs the idempotent
``grade_attempt`` once, so results are never stale (AI-154 AC-05).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session, selectinload

from app.models.course import Class
from app.models.quiz import Quiz, QuizAnswer, QuizAttempt, QuizQuestion
from app.models.user import User
from app.schemas.quiz import (
    QuizAnswerResultRead,
    QuizAttemptHistoryRead,
    QuizAttemptResultRead,
    QuizOptionStudentResultRead,
    QuizQuestionResultRead,
    QuizQuestionStatisticRead,
    QuizStatisticsRead,
    QuizStudentListRead,
    QuizSubmissionRead,
    ScoreBandRead,
)
from app.services.quiz_attempt_service import (
    GRADABLE_ATTEMPT_STATUSES,
    attempt_effective_passing_percentage,
    attempt_percentage,
    grade_attempt,
    passing_score_for,
    round_half_up,
    select_official_attempts,
)
from app.services.quiz_excel import FILL_IN_BLANK_ANSWERS_KEY
from app.services.quiz_grading import parse_fill_in_aliases

# Fixed percentage bands used by the teacher statistics and the Summary sheet.
SCORE_BANDS: tuple[tuple[str, float, float | None], ...] = (
    ("<50", float("-inf"), 50.0),
    ("50-59", 50.0, 60.0),
    ("60-69", 60.0, 70.0),
    ("70-79", 70.0, 80.0),
    ("80-89", 80.0, 90.0),
    ("90-100", 90.0, None),
)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
_HEADER_FONT = Font(bold=True, color="FFFFFF")
_TITLE_FONT = Font(bold=True, size=14)
_THIN_SIDE = Side(style="thin", color="D9D9D9")
_BORDER = Border(left=_THIN_SIDE, right=_THIN_SIDE, top=_THIN_SIDE, bottom=_THIN_SIDE)
_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)


def _graded_attempts(
    db: Session, quiz_id: uuid.UUID, *, with_answers: bool = False
) -> list[QuizAttempt]:
    query = db.query(QuizAttempt).filter(
        QuizAttempt.quiz_id == quiz_id,
        QuizAttempt.status.in_(GRADABLE_ATTEMPT_STATUSES),
    )
    if with_answers:
        query = query.options(selectinload(QuizAttempt.answers))
    return query.all()


def ensure_graded(db: Session, attempts: list[QuizAttempt]) -> bool:
    """Grade submitted attempts that have no result yet (idempotent retry).

    Returns ``True`` when at least one attempt was graded. The caller must
    re-query relationships it already loaded (answers) after a ``True`` result.
    """
    changed = False
    for attempt in attempts:
        if attempt.status in GRADABLE_ATTEMPT_STATUSES and attempt.graded_at is None:
            grade_attempt(db, attempt.id, commit=False)
            changed = True
    if changed:
        db.commit()
    return changed


def _official_attempts(db: Session, quiz_id: uuid.UUID) -> list[QuizAttempt]:
    """One official attempt per student, with answers loaded for statistics."""
    attempts = _graded_attempts(db, quiz_id, with_answers=True)
    if ensure_graded(db, attempts):
        db.expire_all()
        attempts = _graded_attempts(db, quiz_id, with_answers=True)
    return list(select_official_attempts(attempts).values())


def _is_official(db: Session, attempt: QuizAttempt) -> bool:
    siblings = (
        db.query(QuizAttempt)
        .filter(
            QuizAttempt.quiz_id == attempt.quiz_id,
            QuizAttempt.student_id == attempt.student_id,
        )
        .all()
    )
    official = select_official_attempts(siblings)
    selected = official.get(attempt.student_id)
    return selected is not None and selected.id == attempt.id


def _answers_for_attempt(db: Session, attempt: QuizAttempt) -> dict[uuid.UUID, QuizAnswer]:
    rows = db.query(QuizAnswer).filter(QuizAnswer.attempt_id == attempt.id).all()
    return {row.question_id: row for row in rows}


def _correct_option_labels(question: QuizQuestion) -> list[str]:
    return sorted(option.option_label for option in question.options if option.is_correct)


def _accepted_text_answers(question: QuizQuestion) -> list[str]:
    if question.question_type != "FILL_IN_BLANK":
        return []
    config = question.grading_config or {}
    return parse_fill_in_aliases(config.get(FILL_IN_BLANK_ANSWERS_KEY))


def build_history(db: Session, quiz: Quiz, student_id: uuid.UUID) -> list[QuizAttemptHistoryRead]:
    """Every own attempt for a quiz, newest first, with the official one marked."""
    attempts = (
        db.query(QuizAttempt)
        .filter(
            QuizAttempt.quiz_id == quiz.id,
            QuizAttempt.student_id == student_id,
        )
        .order_by(QuizAttempt.started_at.desc())
        .all()
    )
    ensure_graded(db, attempts)
    official = select_official_attempts(attempts).get(student_id)
    return [
        QuizAttemptHistoryRead(
            id=attempt.id,
            quiz_id=attempt.quiz_id,
            status=attempt.status,
            started_at=attempt.started_at,
            submitted_at=attempt.submitted_at,
            graded_at=attempt.graded_at,
            total_score=attempt.total_score,
            max_score=attempt.max_score,
            passing_score=attempt.passing_score,
            passing_percentage=attempt.passing_percentage,
            percentage=attempt_percentage(attempt.total_score, attempt.max_score),
            is_passed=attempt.is_passed,
            official=official is not None and official.id == attempt.id,
        )
        for attempt in attempts
    ]


def build_student_quiz_list(
    db: Session, clazz: Class, student_id: uuid.UUID
) -> list[QuizStudentListRead]:
    """Published quizzes in one class with the student's own attempt summary.

    Only quizzes the student can actually see are returned (``PUBLISHED``);
    drafts stay teacher-only. Submitted attempts are graded lazily just like the
    history/result reads, so the list is correct even before AI-153's submit
    hook runs.
    """
    quizzes = (
        db.query(Quiz)
        .options(selectinload(Quiz.questions))
        .filter(Quiz.class_id == clazz.id, Quiz.status == "PUBLISHED")
        .order_by(Quiz.created_at.desc())
        .all()
    )
    if not quizzes:
        return []

    quiz_ids = [quiz.id for quiz in quizzes]
    attempts = (
        db.query(QuizAttempt)
        .filter(
            QuizAttempt.quiz_id.in_(quiz_ids),
            QuizAttempt.student_id == student_id,
        )
        .all()
    )
    if ensure_graded(db, attempts):
        attempts = (
            db.query(QuizAttempt)
            .filter(
                QuizAttempt.quiz_id.in_(quiz_ids),
                QuizAttempt.student_id == student_id,
            )
            .all()
        )

    by_quiz: dict[uuid.UUID, list[QuizAttempt]] = {}
    for attempt in attempts:
        by_quiz.setdefault(attempt.quiz_id, []).append(attempt)

    rows: list[QuizStudentListRead] = []
    for quiz in quizzes:
        own_attempts = by_quiz.get(quiz.id, [])
        official = select_official_attempts(own_attempts).get(student_id)
        total_points = round_half_up(sum(float(question.points or 0) for question in quiz.questions))
        rows.append(
            QuizStudentListRead(
                id=quiz.id,
                class_id=quiz.class_id,
                title=quiz.title,
                description=quiz.description,
                question_count=len(quiz.questions),
                total_points=total_points,
                time_limit_minutes=quiz.time_limit_minutes,
                passing_score=passing_score_for(quiz, total_points),
                passing_percentage=attempt_effective_passing_percentage(quiz),
                attempt_count=len(own_attempts),
                submitted_count=sum(
                    1
                    for attempt in own_attempts
                    if attempt.status in GRADABLE_ATTEMPT_STATUSES
                ),
                has_in_progress=any(
                    attempt.status == "IN_PROGRESS" for attempt in own_attempts
                ),
                official_attempt_id=official.id if official is not None else None,
                official_score=official.total_score if official is not None else None,
                official_max_score=official.max_score if official is not None else None,
                official_percentage=(
                    attempt_percentage(official.total_score, official.max_score)
                    if official is not None
                    else None
                ),
                official_is_passed=official.is_passed if official is not None else None,
                official_submitted_at=(
                    official.submitted_at if official is not None else None
                ),
            )
        )
    return rows


def build_attempt_result(db: Session, quiz: Quiz, attempt: QuizAttempt) -> QuizAttemptResultRead:
    """Full per-question result for one submitted/graded attempt."""
    answers = _answers_for_attempt(db, attempt)
    show_explanations = bool(quiz.show_explanations) and (
        attempt.status in GRADABLE_ATTEMPT_STATUSES
    )

    questions: list[QuizQuestionResultRead] = []
    for question in quiz.questions:
        answer = answers.get(question.id)
        questions.append(
            QuizQuestionResultRead(
                id=question.id,
                question_type=question.question_type,
                question=question.question,
                points=question.points,
                order_index=question.order_index,
                explanation=question.explanation if show_explanations else None,
                options=[
                    QuizOptionStudentResultRead.model_validate(option)
                    for option in question.options
                ],
                correct_option_labels=_correct_option_labels(question),
                accepted_text_answers=_accepted_text_answers(question),
                answer=(
                    QuizAnswerResultRead(
                        question_id=question.id,
                        selected_option_labels=answer.selected_option_labels,
                        text_answer=answer.text_answer,
                        score_awarded=answer.score_awarded,
                        is_correct=answer.is_correct,
                    )
                    if answer is not None
                    else None
                ),
            )
        )

    return QuizAttemptResultRead(
        id=attempt.id,
        quiz_id=attempt.quiz_id,
        quiz_title=quiz.title,
        student_id=attempt.student_id,
        status=attempt.status,
        started_at=attempt.started_at,
        submitted_at=attempt.submitted_at,
        graded_at=attempt.graded_at,
        total_score=attempt.total_score,
        max_score=attempt.max_score,
        passing_score=attempt.passing_score,
        passing_percentage=attempt.passing_percentage,
        percentage=attempt_percentage(attempt.total_score, attempt.max_score),
        is_passed=attempt.is_passed,
        official=_is_official(db, attempt),
        show_explanations=show_explanations,
        questions=questions,
    )


def build_submissions(db: Session, quiz: Quiz) -> list[QuizSubmissionRead]:
    """All submitted/expired attempts with student identity and official flag."""
    attempts = (
        db.query(QuizAttempt)
        .filter(
            QuizAttempt.quiz_id == quiz.id,
            QuizAttempt.status.in_(GRADABLE_ATTEMPT_STATUSES),
        )
        .order_by(QuizAttempt.submitted_at.asc())
        .all()
    )
    ensure_graded(db, attempts)
    official_by_student = select_official_attempts(attempts)

    students = {
        user.id: user
        for user in db.query(User).filter(User.id.in_({attempt.student_id for attempt in attempts}))
    }
    rows: list[QuizSubmissionRead] = []
    for attempt in attempts:
        student = students.get(attempt.student_id)
        official = official_by_student.get(attempt.student_id)
        rows.append(
            QuizSubmissionRead(
                attempt_id=attempt.id,
                student_id=attempt.student_id,
                student_name=student.full_name if student else "",
                student_code=student.student_code if student else None,
                student_email=student.email if student else "",
                status=attempt.status,
                submitted_at=attempt.submitted_at,
                graded_at=attempt.graded_at,
                total_score=attempt.total_score,
                max_score=attempt.max_score,
                passing_percentage=attempt.passing_percentage,
                percentage=attempt_percentage(attempt.total_score, attempt.max_score),
                is_passed=attempt.is_passed,
                official=official is not None and official.id == attempt.id,
            )
        )
    return rows


def _band_for(percentage: float | None) -> str:
    if percentage is None:
        return SCORE_BANDS[0][0]
    for label, minimum, maximum in SCORE_BANDS:
        if maximum is None:
            return label
        if minimum <= percentage < maximum:
            return label
    return SCORE_BANDS[-1][0]


def _has_answer(answer: QuizAnswer | None) -> bool:
    if answer is None:
        return False
    return bool(answer.selected_option_labels) or bool((answer.text_answer or "").strip())


def build_statistics(db: Session, quiz: Quiz) -> QuizStatisticsRead:
    """Class statistics from one official attempt per student (AI-154 AC-04)."""
    official = _official_attempts(db, quiz.id)
    submitted_count = len(official)
    max_score = round_half_up(sum(float(question.points) for question in quiz.questions))

    percentages = [
        attempt_percentage(attempt.total_score, attempt.max_score) for attempt in official
    ]
    valid_percentages = [value for value in percentages if value is not None]
    total_scores = [attempt.total_score for attempt in official if attempt.total_score is not None]

    average_score = round_half_up(sum(total_scores) / len(total_scores)) if total_scores else None
    average_percentage = (
        round_half_up(sum(valid_percentages) / len(valid_percentages))
        if valid_percentages
        else None
    )
    passed = sum(1 for attempt in official if attempt.is_passed)
    pass_rate = round_half_up((passed / submitted_count) * 100) if submitted_count else None

    band_counts = {label: 0 for label, _, _ in SCORE_BANDS}
    for percentage in percentages:
        band_counts[_band_for(percentage)] += 1
    score_bands = [
        ScoreBandRead(band=label, count=band_counts[label]) for label, _, _ in SCORE_BANDS
    ]

    answers_by_attempt = [
        {answer.question_id: answer for answer in attempt.answers} for attempt in official
    ]
    per_question: list[QuizQuestionStatisticRead] = []
    for question in quiz.questions:
        answered_count = 0
        full_correct = 0
        score_total = 0.0
        for answers in answers_by_attempt:
            answer = answers.get(question.id)
            if _has_answer(answer):
                answered_count += 1
            score_total += float(answer.score_awarded or 0.0) if answer is not None else 0.0
            if answer is not None and answer.is_correct:
                full_correct += 1
        per_question.append(
            QuizQuestionStatisticRead(
                question_id=question.id,
                order_index=question.order_index,
                question_type=question.question_type,
                question=question.question,
                points=question.points,
                answered_count=answered_count,
                average_score=(
                    round_half_up(score_total / submitted_count) if submitted_count else 0.0
                ),
                full_correct_rate=(
                    round_half_up((full_correct / submitted_count) * 100)
                    if submitted_count
                    else 0.0
                ),
            )
        )

    return QuizStatisticsRead(
        quiz_id=quiz.id,
        quiz_title=quiz.title,
        submitted_count=submitted_count,
        max_score=max_score,
        average_score=average_score,
        average_percentage=average_percentage,
        pass_rate=pass_rate,
        score_bands=score_bands,
        per_question=per_question,
    )


def _style_header(ws, row: int = 1) -> None:
    for cell in ws[row]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER
        cell.border = _BORDER


def _autosize(ws, widths: dict[str, int] | None = None) -> None:
    for column_cells in ws.columns:
        letter = get_column_letter(column_cells[0].column)
        if widths and letter in widths:
            ws.column_dimensions[letter].width = widths[letter]
            continue
        longest = max(
            (len(str(cell.value)) for cell in column_cells if cell.value is not None), default=0
        )
        ws.column_dimensions[letter].width = min(max(longest + 2, 10), 60)


def _fmt_dt(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _write_summary_sheet(ws, quiz: Quiz, clazz: Class, statistics: QuizStatisticsRead) -> None:
    ws["A1"] = "Quiz Results Summary"
    ws["A1"].font = _TITLE_FONT
    ws.merge_cells("A1:B1")

    rows: list[tuple[str, object]] = [
        ("Class", f"{clazz.code} - {clazz.name}"),
        ("Quiz", quiz.title),
        ("Exported at (UTC)", _fmt_dt(datetime.now(UTC))),
        ("Max score", statistics.max_score),
        ("Students submitted", statistics.submitted_count),
        ("Average score", statistics.average_score),
        ("Average percentage (%)", statistics.average_percentage),
        ("Pass rate (%)", statistics.pass_rate),
    ]
    row_index = 3
    ws.cell(row=row_index, column=1, value="Field")
    ws.cell(row=row_index, column=2, value="Value")
    _style_header(ws, row_index)
    for label, value in rows:
        row_index += 1
        ws.cell(row=row_index, column=1, value=label).alignment = _LEFT
        cell = ws.cell(row=row_index, column=2, value=value)
        cell.alignment = _LEFT
        if isinstance(value, float):
            cell.number_format = "0.00"

    row_index += 2
    ws.cell(row=row_index, column=1, value="Score band (%)")
    ws.cell(row=row_index, column=2, value="Students")
    _style_header(ws, row_index)
    for band in statistics.score_bands:
        row_index += 1
        ws.cell(row=row_index, column=1, value=band.band).alignment = _CENTER
        ws.cell(row=row_index, column=2, value=band.count).alignment = _CENTER

    _autosize(ws, {"A": 28, "B": 48})


def _write_submissions_sheet(ws, submissions: list[QuizSubmissionRead]) -> None:
    headers = [
        "Student Code",
        "Student Name",
        "Email",
        "Status",
        "Submitted At (UTC)",
        "Graded At (UTC)",
        "Score",
        "Max Score",
        "Percentage (%)",
        "Passed",
        "Official",
    ]
    ws.append(headers)
    _style_header(ws)
    for submission in submissions:
        ws.append(
            [
                submission.student_code or "",
                submission.student_name,
                submission.student_email,
                submission.status,
                _fmt_dt(submission.submitted_at),
                _fmt_dt(submission.graded_at),
                submission.total_score,
                submission.max_score,
                submission.percentage,
                "Yes" if submission.is_passed else "No",
                "Yes" if submission.official else "No",
            ]
        )
        score_row = ws.max_row
        for column in (7, 8, 9):
            ws.cell(row=score_row, column=column).number_format = "0.00"
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = _CENTER if cell.column >= 4 else _LEFT
    ws.freeze_panes = "A2"
    _autosize(ws)


def _write_per_question_sheet(ws, statistics: QuizStatisticsRead) -> None:
    headers = [
        "Order",
        "Question Type",
        "Question",
        "Points",
        "Answered",
        "Average Score",
        "Full-Correct Rate (%)",
    ]
    ws.append(headers)
    _style_header(ws)
    for stat in statistics.per_question:
        ws.append(
            [
                stat.order_index + 1,
                stat.question_type,
                stat.question,
                stat.points,
                stat.answered_count,
                stat.average_score,
                stat.full_correct_rate,
            ]
        )
        row = ws.max_row
        for column in (4, 6, 7):
            ws.cell(row=row, column=column).number_format = "0.00"
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = _LEFT if cell.column == 3 else _CENTER
    ws.freeze_panes = "A2"
    _autosize(ws, {"C": 60})


def build_results_workbook(
    quiz: Quiz,
    clazz: Class,
    submissions: list[QuizSubmissionRead],
    statistics: QuizStatisticsRead,
) -> openpyxl.Workbook:
    """Build the AI-154 class results workbook: Summary, Submissions, PerQuestion."""
    workbook = openpyxl.Workbook()
    summary = workbook.active
    summary.title = "Summary"
    _write_summary_sheet(summary, quiz, clazz, statistics)
    _write_submissions_sheet(workbook.create_sheet("Submissions"), submissions)
    _write_per_question_sheet(workbook.create_sheet("PerQuestion"), statistics)
    return workbook
