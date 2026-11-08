# AI-153 — Student Quiz Taking with Timer, Autosave, and Auto-submit

> **Ticket:** [AI-153](https://ai03.atlassian.net/browse/AI-153) (Epic AI-147: Quiz Generation & Assessment) | **Priority:** High  
> **Target Branch:** `features/Implementation_StudentQuizTaking`  
> **Related Stories:** AI-148 (schema & Excel contract), AI-151 (teacher review/approve/export), AI-152 (Excel import), AI-154 (auto-grade reporting).

---

## 1. Purpose and Scope

AI-148/151/152 produce a `PUBLISHED` quiz that a teacher can review and export, but a
student still cannot take it. AI-153 closes that gap:

- list published quizzes for the classes the student is enrolled in;
- open a quiz inside its availability window for a limited number of attempts;
- count down from **server time**, navigate, mark questions, and see progress;
- autosave answers as a draft, so a reload or reconnect restores the work;
- auto-submit when time runs out and require a warning before a manual submit;
- never expose correct answers/explanations/rubrics before submission;
- make repeated submit clicks idempotent (one attempt, one score).

Out of scope (mentor brainstorming notes, tracked separately): question-bank
versioning/snapshots (Coderbyte style), configurable scoring presets (2/3 partial credit,
double-penalty), the full Bloom taxonomy, skill taxonomy, leave/calendar tables, and
ticket expiry. See §9.

---

## 2. Acceptance Criteria → Implementation

| AC | Implementation |
| --- | --- |
| AC-01 | `GET /api/student/quizzes[?class_id=]` returns published quizzes in enrolled classes with `opens_at`, `closes_at`, `attempts_used`, `attempts_left`, `availability`, and the active/latest attempt. |
| AC-02 | `POST /api/student/quizzes/{quiz_id}/attempts` returns `server_time`, `deadline_at`, and `remaining_seconds`. The client computes a server-clock offset and renders the countdown from that; the server re-checks the deadline on every request. Question grid supports navigation, mark-for-review, answered count. |
| AC-03 | `PATCH /api/student/attempts/{id}/answers` upserts drafts (`selected_option_labels`, `text_answer`, `is_marked_for_review`). The player debounces autosave, flushes on tab hide/online, and reloads the draft via `GET /api/student/attempts/{id}`. |
| AC-04 | The client auto-submits at zero and shows an unanswered-question warning in the manual submit dialog. `submit_attempt` also closes an overdue attempt itself, so closing/pausing the tab cannot extend the deadline. |
| AC-05 | The exam DTO (`QuizQuestionStudentExamRead`) only contains question text, type, points, difficulty, order, and option label/text. `is_correct`, `explanation`, `rubric`, `source_reference`, `grading_config` appear only in the result DTO after submission. |
| AC-06 | Submit locks the attempt row, grades exactly once, and returns the stored result for any later call. A partial unique index on `(quiz_id, student_id) WHERE status = 'IN_PROGRESS'` also prevents two tabs from consuming two attempts. |

---

## 3. Data Model Changes (migration `0040_quiz_availability`)

`quiz`:

| Column | Type | Meaning |
| --- | --- | --- |
| `opens_at` | `timestamptz NULL` | Earliest start; `NULL` = no lower bound. |
| `closes_at` | `timestamptz NULL` | Latest start; `NULL` = no upper bound. |
| `max_attempts` | `integer NULL` | Attempts a student may start; `NULL` = unlimited. Default `1`. |

Constraints: `opens_at < closes_at` when both are present; `max_attempts > 0` or `NULL`.

`quiz_answer`:

| Column | Type | Meaning |
| --- | --- | --- |
| `is_marked_for_review` | `boolean NOT NULL DEFAULT false` | Persisted so a reload keeps the student's review marks. |

`quiz_attempt` gets a partial unique index `uq_quiz_attempt_active` on
`(quiz_id, student_id)` where `status = 'IN_PROGRESS'`.

`passing_percentage` is the percentage threshold (0–100) and the source of truth for
pass/fail; it defaults to `50` and is `NOT NULL` on `quiz`. AI-154 keeps `passing_score`
as a cached point conversion for display. A submitted attempt is passed when
`max(0, total/max_total*100) >= passing_percentage`; grading snapshots both values on
`quiz_attempt`, so editing the quiz later cannot change an already-graded result.

---

## 4. API Contract

All routes require a student/trainer JWT and are scoped to the caller's active
enrollment. An attempt belonging to another user is `404`, not `403`.

### 4.1 List

```
GET /api/student/quizzes?class_id=<optional>
```

```json
{
  "server_time": "2026-11-07T03:00:00Z",
  "quizzes": [
    {
      "quiz_id": "…",
      "class_id": "…",
      "class_code": "AI201",
      "class_name": "AI Core",
      "title": "Networking midterm",
      "description": "…",
      "time_limit_minutes": 30,
      "opens_at": "2026-11-07T02:00:00Z",
      "closes_at": "2026-11-07T04:00:00Z",
      "max_attempts": 2,
      "attempts_used": 0,
      "attempts_left": 2,
      "availability": "OPEN",
      "attempt_id": null,
      "attempt_status": null,
      "can_start": true,
      "total_score": null,
      "is_passed": null
    }
  ]
}
```

`availability` is `UPCOMING | OPEN | CLOSED | EXHAUSTED`. `attempts_left` is `null`
for unlimited attempts. `can_start` is true for an active attempt even after the window
closes, so the student can finish what they already started.

### 4.2 Start / resume

```
POST /api/student/quizzes/{quiz_id}/attempts
```

Returns the active attempt, starts a new one, or returns an auto-finalized attempt with
its result. Rejected with `409` when the quiz is upcoming, closed, has no attempts left,
or when the quiz is not `PUBLISHED` (`404`).

The list's “View result” action does **not** call this endpoint: it opens
`/student/class/{class_id}/quizzes/{quiz_id}/result/{attempt_id}` which uses
`GET /api/student/attempts/{attempt_id}` so a stored result can be reviewed without
consuming another attempt. “Try again” calls `POST …/attempts`.

### 4.3 Session shape (active)

```json
{
  "attempt_id": "…",
  "quiz_id": "…",
  "quiz_title": "…",
  "quiz_description": "…",
  "time_limit_minutes": 30,
  "status": "IN_PROGRESS",
  "started_at": "…",
  "deadline_at": "…",
  "server_time": "…",
  "remaining_seconds": 1799,
  "questions": [
    {
      "id": "…",
      "question_type": "SINGLE_CHOICE",
      "question": "…",
      "points": 2,
      "difficulty": "APPLY",
      "order_index": 0,
      "options": [
        {"id": "…", "option_label": "A", "option_text": "…", "order_index": 0}
      ]
    }
  ],
  "answers": [
    {
      "id": "…",
      "attempt_id": "…",
      "question_id": "…",
      "selected_option_labels": ["A"],
      "text_answer": null,
      "is_marked_for_review": true,
      "score_awarded": null,
      "breakdown": null
    }
  ],
  "total_points": 10.0,
  "answered_count": 1,
  "result": null
}
```

After submission the same shape has `status = SUBMITTED|EXPIRED`, empty `questions`,
and a populated `result` with per-question `is_correct`, `explanation`, `rubric`,
`source_reference`, and per-answer `score_awarded`/`breakdown`.

### 4.4 Autosave

```
PATCH /api/student/attempts/{attempt_id}/answers
{"answers": [{"question_id": "…", "selected_option_labels": ["A","C"], "text_answer": null, "is_marked_for_review": false}]}
```

- Omitted questions keep their previous draft.
- Empty labels + empty text + not marked deletes the draft row.
- A single-choice question rejects more than one label; a fill-in question rejects
  option labels; a choice question rejects a text answer (`422`).
- After the deadline the endpoint finalizes the attempt (auto-submit) and returns `409`;
  the client then reloads the session to show the result.
- After submission the endpoint returns `409`.

### 4.5 Reload

```
GET /api/student/attempts/{attempt_id}
```

Returns the draft session. If the attempt is overdue it is auto-submitted first, so a
refresh after the deadline shows the result instead of an editable form.

### 4.6 Submit

```
POST /api/student/attempts/{attempt_id}/submit
{"answers": [...]}   // optional final flush
```

- Locks `quiz_attempt` with `SELECT … FOR UPDATE` on PostgreSQL.
- If already `SUBMITTED`/`EXPIRED`, returns the stored session/result (AC-06).
- Otherwise applies any final answers, grades every question with the AI-148 grading
  engine, stores `total_score`/`is_passed`/`submitted_at`, and returns the result.
- A submit that lands more than 5 seconds after the deadline is stored as `EXPIRED`,
  but still keeps the client's final answers.

---

## 5. Timer Protocol

1. The session response carries `server_time` and `deadline_at`.
2. The client stores `offset = server_time - client_now`.
3. Every second it renders `max(0, floor((deadline_at - (client_now + offset)) / 1000))`.
4. At zero it calls submit once; a ref guards against duplicate calls.
5. The server never trusts the client clock: `save_draft`, `submit`, and `get_attempt`
   all compare the database `started_at + time_limit_minutes` with the server clock.
6. An attempt whose window closed but whose personal deadline has not passed remains
   resumable; the quiz window only gates *new* attempts.

---

## 6. AC-05 Data Exposure

The exam view is a separate Pydantic model, not a filtered teacher model. It does not
have fields for correct answers, explanations, rubrics, source references, or grading
configuration, so a serialization change cannot accidentally leak them. Router tests
serialize the whole exam payload and assert the forbidden keys are absent.

The result view is only reachable when `attempt.status != 'IN_PROGRESS'`.

---

## 7. AC-06 Concurrency

- Every submit/autosave first loads the attempt with `FOR UPDATE`.
- The second submit waits, sees `SUBMITTED`, and returns the same attempt id and score.
- The partial unique index prevents two simultaneous `POST /attempts` calls from
  creating two active attempts; the loser re-reads and resumes the winner.
- The client also disables the submit button and uses an in-flight ref.

---

## 8. Teacher Configuration

The draft-only metadata form now exposes:

- `opens_at` / `closes_at` (`datetime-local`, empty = no bound);
- `max_attempts` (empty = unlimited);
- `passing_percentage` is the pass threshold (0–100, default 50); `passing_score` is shown
  as the converted point value.

The merged window is validated server-side, so a PATCH containing only one bound cannot
produce `opens_at >= closes_at`.

---

## 9. Mentor Notes → Follow-ups

The mentor's meeting notes contain several ideas that are deliberately **not** part of
AI-153:

| Idea | Why it is separate |
| --- | --- |
| Coderbyte-style question-bank snapshots; iMocha-style re-scoring | Requires a versioned question-bank model and a decision on whether historical attempts re-score. AI-153 deliberately grades against the approved quiz snapshot stored in `quiz_question`/`quiz_option`, which AI-151 freezes after approval. |
| Scoring presets: “≥2/3 correct”, “wrong answer costs double” | The AI-148 grading engine already supports `partial_credit` and `penalty_ratio`; exposing presets is a teacher-configuration story, not a delivery story. |
| Bloom 6 levels / skill taxonomy | The schema currently allows `REMEMBER/UNDERSTAND/APPLY`; expanding taxonomy and mapping to skills is a curriculum-analytics epic. |
| Aggregating frequent student questions into Q&A | Content-gap/Q&A feature, not quiz delivery. |
| Leave types (annual/sick/maternity), ticket expiry, calendar table | HR/attendance and ticket-lifecycle work; unrelated to AI-153. |

---

## 10. Verification

- `uv run ruff check app tests/quizzes/test_student_quiz_router.py`
- `uv run pytest tests/quizzes/test_student_quiz_router.py` — 11 cases covering list,
  window rejection, answer hiding, autosave/reload/clear, grading, idempotent submit,
  attempt limit, timeout auto-submit, cross-student access, invalid payloads, duplicate
  start, and teacher window configuration.
- `npm test -- --run` — 216 tests including the timer/autosave helper suite.
- `npm run build` — student quiz list and player routes compile.
