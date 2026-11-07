# AI-154 — Auto-grade Objective Quiz Attempts and Show Results with Class Statistics

> **Ticket:** [AI-154](https://ai03.atlassian.net/browse/AI-154) (Epic AI-147: Quiz Generation & Assessment) | **Priority:** Medium
> **Assignee:** nguyenvu040905
> **Sprint:** Sprint 4 — Quiz & Attendance
> **Status:** Implemented (AI-153 submit hook pending)
> **Target Branch:** `features/Implementation_QuizAutoGrade`
> **Related Stories:** AI-148 (quiz schema & grading contract), AI-149 (generate quiz), AI-150 (validate questions), AI-151 (review/approve/export), AI-152 (Excel import), AI-153 (student quiz-taking).

---

## 1. Purpose and Scope

AI-148 defines the objective scoring contract; AI-151 lets teachers publish an
approved quiz; AI-153 owns the student attempt lifecycle (start, autosave,
submit, auto-submit). AI-154 closes the loop:

1. **Grade an attempt** with the AI-148 contract and snapshot the attempt totals.
2. **Student result & history** — scores, percentage, pass flag, per-question
   selected/correct answers, gated explanations, and the official attempt marker.
3. **Teacher submissions, statistics and Excel export** — one row per submitted
   attempt plus class-level statistics built from one official attempt per student,
   exported as `Summary`, `Submissions`, `PerQuestion`.
4. **Integrity** — idempotent row-locked grading, answer-contract validation, and
   no scores/answers/explanations before submission.

### Out of Scope

* Attempt lifecycle, timer, autosave, auto-submit, and multiple-submit dedupe (AI-153).
* Subjective/rubric grading, `PENDING` status, teacher override and audit trail.
* New question types.
* Frontend screens.

### Acceptance Criteria Mapping

| AC | Where it is implemented |
|---|---|
| AC-01 objective scoring | `app/services/quiz_grading.py` (AI-148), called by `quiz_attempt_service._grade_question` |
| AC-02 attempt totals | `quiz_attempt_service.grade_attempt` — snapshot, ROUND_HALF_UP 2dp, default 50% passing score, negative preserved |
| AC-03 student result & history | `GET /api/student/quizzes/{quiz_id}/attempts`, `GET .../attempts/{attempt_id}/result` |
| AC-04 teacher submissions, statistics, export | `GET /api/classes/{class_id}/quizzes/{quiz_id}/submissions|statistics|results/export` |
| AC-05 integrity | row-locked idempotent `grade_attempt`, full-attempt validation before mutation, lazy retry on read, ownership-scoped student routes |

---

## 2. Key Technical Decisions & Rationale

| # | Decision | Why |
|---|---|---|
| **D1** | **Grading stays in `quiz_attempt_service`; the new module is read-only.** | AI-148/153 already own the scoring contract and submit lifecycle. One grading implementation prevents score drift between submit, result, and statistics. |
| **D2** | **Read paths lazily re-run `grade_attempt` when `graded_at` is null.** | AI-153 calls grading after submit, but a crash between submit and grading (or a retried grading job) must not hide results. `grade_attempt` is idempotent and row-locked, so the retry is safe. Every read path re-queries after grading (AC-05 retryability). |
| **D3** | **Official attempt = highest graded score, ties go to the latest submission.** | AI-154 AC-03/AC-04. Implemented once in `select_official_attempts`, consumed by history, submissions, and statistics. |
| **D4** | **Correct answers are revealed after submission; explanations need `quiz.show_explanations`.** | AC-03. Result options carry `is_correct`; `explanation` is nulled unless the quiz opts in. `IN_PROGRESS` attempts return `409` instead of leaking answers (also AI-153 AC-05). |
| **D5** | **Student routes are ownership-scoped, not role-scoped.** | A caller can read only attempts whose `student_id` matches the JWT user; other attempts return `404` (no existence leak). |
| **D6** | **Teacher routes reuse `_require_class_teacher`.** | Same rule as AI-151/AI-152: admin, primary trainer, or co-trainer (`class_trainer`). No new authorization code path. |
| **D7** | **Statistics use the snapshot, not recomputation from the quiz.** | `total_score`, `max_score`, `passing_score`, and `is_passed` were frozen at grading time; editing the quiz later must not rewrite history. Per-question `max_points` remains the question's points. |
| **D8** | **Fixed percentage bands.** | `<50, 50-59, 60-69, 70-79, 80-89, 90-100`, aligned with the usual Vietnamese grading scale. Negative totals land in `<50`; values above 100 in `90-100`. |
| **D9** | **New routers instead of editing the AI-151 router.** | `quizzes/results.py` (teacher) and `quizzes/student_results.py` (student) keep AI-154 routes out of files AI-153 will touch, reducing merge conflicts. |
| **D10** | **Excel export is a new workbook builder.** | `quiz_excel.py` exports the question paper (AI-148/151). Results are a different workbook with `Summary`, `Submissions`, `PerQuestion`; keeping them separate avoids overloading the template contract. |

---

## 3. API Design

### 3.1 Student attempt history

```http
GET /api/student/quizzes/{quiz_id}/attempts
Authorization: Bearer <student token>
```

Returns every own attempt, newest `started_at` first. Graded attempts carry
`total_score`, `max_score`, `passing_score`, `percentage`, `is_passed`; exactly
one graded attempt has `official: true`.

```json
[
  {
    "id": "…",
    "quiz_id": "…",
    "status": "SUBMITTED",
    "started_at": "2026-11-05T08:00:00Z",
    "submitted_at": "2026-11-05T09:30:00Z",
    "graded_at": "2026-11-05T09:30:01Z",
    "total_score": 6.0,
    "max_score": 6.0,
    "passing_score": 3.0,
    "percentage": 100.0,
    "is_passed": true,
    "official": true
  }
]
```

| Status | When |
|---|---|
| `200` | List returned (possibly empty) |
| `404` | Quiz does not exist |
| `409` | A submitted attempt cannot be graded (invalid persisted answers) |

### 3.2 Student attempt result

```http
GET /api/student/quizzes/{quiz_id}/attempts/{attempt_id}/result
Authorization: Bearer <student token>
```

```json
{
  "id": "…",
  "quiz_id": "…",
  "quiz_title": "Auto-grade Quiz",
  "student_id": "…",
  "status": "SUBMITTED",
  "started_at": "…",
  "submitted_at": "…",
  "graded_at": "…",
  "total_score": 6.0,
  "max_score": 6.0,
  "passing_score": 3.0,
  "percentage": 100.0,
  "is_passed": true,
  "official": true,
  "show_explanations": false,
  "questions": [
    {
      "id": "…",
      "question_type": "SINGLE_CHOICE",
      "question": "Binary search complexity?",
      "points": 2.0,
      "order_index": 0,
      "explanation": null,
      "options": [
        { "id": "…", "option_label": "A", "option_text": "O(log n)", "is_correct": true, "order_index": 0 },
        { "id": "…", "option_label": "B", "option_text": "O(n)", "is_correct": false, "order_index": 1 }
      ],
      "correct_option_labels": ["A"],
      "accepted_text_answers": [],
      "answer": {
        "question_id": "…",
        "selected_option_labels": ["A"],
        "text_answer": null,
        "score_awarded": 2.0,
        "is_correct": true
      }
    }
  ]
}
```

* `explanation` is populated only when `quiz.show_explanations = true`.
* FILL_IN_BLANK questions have `options: []` and `accepted_text_answers` parsed
  from `grading_config["correct_answers"]` (pipe/newline aliases).
* `answer` is `null` only if grading has not materialised a row (normally every
  question is materialised, including unanswered ones with score `0`).

| Status | When |
|---|---|
| `200` | Result returned |
| `404` | Quiz/attempt missing **or** the attempt belongs to another student |
| `409` | Attempt is `IN_PROGRESS`, or grading rejected persisted answers |

### 3.3 Teacher submissions

```http
GET /api/classes/{class_id}/quizzes/{quiz_id}/submissions
Authorization: Bearer <trainer/admin token>
```

Returns every `SUBMITTED`/`EXPIRED` attempt ordered by `submitted_at`, including
student code/name/email, score, percentage, pass flag, and `official` flag.
Only the class' primary trainer, co-trainers, or an admin may call it (`403`
otherwise; `404` when the quiz does not belong to the class).

### 3.4 Teacher statistics

```http
GET /api/classes/{class_id}/quizzes/{quiz_id}/statistics
```

```json
{
  "quiz_id": "…",
  "quiz_title": "Auto-grade Quiz",
  "submitted_count": 2,
  "max_score": 6.0,
  "average_score": 5.25,
  "average_percentage": 87.5,
  "pass_rate": 100.0,
  "score_bands": [
    { "band": "<50", "count": 0 },
    { "band": "50-59", "count": 0 },
    { "band": "60-69", "count": 0 },
    { "band": "70-79", "count": 1 },
    { "band": "80-89", "count": 0 },
    { "band": "90-100", "count": 1 }
  ],
  "per_question": [
    {
      "question_id": "…",
      "order_index": 0,
      "question_type": "SINGLE_CHOICE",
      "question": "Binary search complexity?",
      "points": 2.0,
      "answered_count": 2,
      "average_score": 2.0,
      "full_correct_rate": 100.0
    }
  ]
}
```

**Definitions** (all values rounded 2dp, ROUND_HALF_UP):

* `submitted_count` — students with an official graded attempt (one per student).
* `average_score` — mean `attempt.total_score` over official attempts.
* `average_percentage` — mean `attempt_percentage(total_score, max_score)`.
* `pass_rate` — `passed / submitted_count * 100`.
* `score_bands` — distribution of official percentages over the fixed bands.
* `answered_count` — official attempts with a non-empty option selection or text.
* `average_score` (per question) — mean `score_awarded` (missing rows count as 0).
* `full_correct_rate` — attempts with `is_correct = true` / `submitted_count * 100`.

### 3.5 Teacher results export

```http
GET /api/classes/{class_id}/quizzes/{quiz_id}/results/export
```

Streams `quiz-{quiz_id}-results.xlsx`:

| Sheet | Contents |
|---|---|
| `Summary` | Class, quiz, export timestamp, max score, submitted count, average score/percentage, pass rate, band distribution |
| `Submissions` | Student code, name, email, status, submitted/graded at, score, max score, percentage, passed, official |
| `PerQuestion` | Order, type, question, points, answered count, average score, full-correct rate |

### 3.6 Student class quiz list

```http
GET /api/student/classes/{class_id}/quizzes
Authorization: Bearer <student token>
```

Entry point for the student workspace `Quizzes` tab. Returns **published** quizzes
only, newest first, each with the caller's own attempt summary. Drafts stay
teacher-only and no class-wide results leak into this payload.

```json
[
  {
    "id": "…",
    "class_id": "…",
    "title": "C++ Fundamentals - Midterm Quiz",
    "description": "…",
    "question_count": 6,
    "total_points": 8.0,
    "time_limit_minutes": 30,
    "passing_score": 4.0,
    "attempt_count": 3,
    "submitted_count": 2,
    "has_in_progress": true,
    "official_attempt_id": "…",
    "official_score": 6.0,
    "official_max_score": 8.0,
    "official_percentage": 75.0,
    "official_is_passed": true,
    "official_submitted_at": "2026-11-05T09:30:00Z"
  }
]
```

| Status | When |
|---|---|
| `200` | List returned (possibly empty) |
| `403` | Caller has no `ACTIVE` enrollment in the class |
| `404` | Class does not exist |

Submitted attempts are lazily graded on this read too, so the list stays correct
before AI-153's submit hook exists.

---

## 4. Data and Scoring Contract

* Migration `0039_quiz_grading_results` adds `quiz.show_explanations`,
  `quiz_attempt.max_score/passing_score/graded_at`, `quiz_answer.is_correct`.
  It chains **after** `0039_quiz_approval` so Alembic keeps a single head.
* `grade_attempt` validates every answer before mutating: at most one label for
  SINGLE_CHOICE, no duplicate labels, and every label must belong to the question.
  A failed validation raises and leaves `total_score` null → retryable.
* `total_score`, `max_score`, and each `score_awarded` are rounded 2dp
  HALF_UP; `percentage = total_score / max_score * 100` (null when max is 0).
* Passing score snapshots `quiz.passing_score` or defaults to 50% of `max_score`.
* `is_passed = total_score >= passing_score`; negative totals are preserved.

---

## 5. AI-153 Integration Point

AI-153's submit and auto-submit handlers must call, inside the submit transaction:

```python
grade_attempt(db, attempt.id, commit=False)
```

`commit=False` keeps grading atomic with the status transition: if the submit
transaction rolls back, no partial score is written. If AI-153 cannot call it
synchronously, the AI-154 read paths will lazily grade on first read, but the
submit hook is still required for "immediate feedback" semantics.

---

## 6. Test Plan

`tests/quizzes/test_quiz_attempt_grading.py` — 16 unit tests for the scoring
contract, snapshots, idempotence, retryability, and official selection.

`tests/quizzes/test_quiz_results_router.py` - 15 API tests:

* history marks exactly one official attempt and includes in-progress rows;
* result exposes scores, selected answers, correct labels and options;
* explanation hidden unless `show_explanations`, then shown;
* another student's attempt → `404`; unsubmitted → `409`;
* lazy grading of a submitted but ungraded attempt;
* teacher submissions include official flags and student identity;
* statistics use one official attempt per student, per-question rates, bands;
* co-trainer allowed, other trainer/student → `403`;
* export workbook has the three expected sheets and rows;
* unknown quiz → `404` on both surfaces.

* student class quiz list returns published quizzes with own attempt summary,
  hides drafts, lazily grades, and rejects non-members with `403`.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\quizzes -q
.\.venv\Scripts\python.exe -m ruff check .
```
