# AI-149 / AI-151 — Quiz Generation from Question Bank & Blueprint Matrix

> **Ticket:** [AI-149](https://ai03.atlassian.net/browse/AI-149) / [AI-151](https://ai03.atlassian.net/browse/AI-151) (Epic AI-147: Quiz Generation & Assessment) | **Priority:** High
> **Sprint:** Sprint 4 — Quiz & Attendance
> **Status:** Implemented & Verified
> **Target Branch:** `features/Implementation_QuizGenerator`
> **Related Stories:** AI-148 (quiz schema & grading contract), AI-150 (validate questions), AI-153 (student quiz-taking), AI-154 (auto-grade results), AI-159 (skill taxonomy & rubrics), AI-160 (question bank).

---

## 1. Purpose and Scope

Rather than generating unstructured quizzes with ad-hoc grocery-shopping distribution, AI-149/AI-151 establishes a standardized, competency-matrix-driven assessment architecture. Teachers assemble quizzes from the curated class Question Bank (AI-160) across target skills and the 5 Dreyfus cognitive levels (`NOVICE`, `BEGINNER`, `COMPETENT`, `PROFICIENT`, `MASTER`).

Key Capabilities:
1. **Blueprint Availability Matrix:** Real-time query of Question Bank inventory mapped per `(skill, level)` cell.
2. **Standardized Matrix Sampling:** Uniform question quotas per evaluated level ($k \ge 1$, default 3) to guarantee statistically representative skill measurement.
3. **On-Demand AI Fallback:** When bank inventory has fewer questions than requested, the engine automatically synthesizes missing questions from class documents.
4. **Draft Question Swap & Regeneration:** Teachers can replace or re-prompt AI generation for individual draft questions prior to publishing.
5. **Dreyfus Gated Skill Radar:** Upon student submission, the attempt is evaluated against the Dreyfus gating model to calculate achieved levels, ceiling levels, foundation gaps, and visual radar metrics.
6. **Student Solution Visibility Control:** Toggable option (`show_solutions_after_submission: bool`) enabling instructors to withholding correct keys and explanations while preserving the Skill Radar report.

### Acceptance Criteria Mapping

| AC | Requirement | Implementation Module |
|---|---|---|
| **AC-01** | Target skills and uniform blueprint matrix configuration | `app/schemas/quiz.py` (`QuizGenerateBlueprintRequest`) |
| **AC-02** | Bank question sampling ($k$ per evaluated level) | `app/services/quiz_generator_service.py` (`generate_quiz_from_blueprint`) |
| **AC-03** | Auto-generate missing questions from documents | `question_bank_service.generate_questions_from_documents` integration |
| **AC-04** | Single-choice validation, question & option shuffling | `generate_quiz_from_blueprint` (`random.shuffle`, option indexing) |
| **AC-05** | Question swap and AI regeneration in draft mode | `app/services/quiz_generator_service.py` (`swap_or_regenerate_question`) |
| **AC-06** | Student Dreyfus Skill Radar report with foundation gaps | `compute_skill_radar_report` attached to `QuizTakeResultRead` |
| **AC-07** | Solution visibility toggle | `Quiz.show_solutions_after_submission`, migration `0045_quiz_show_solutions.py`, `_result()` sanitization |

---

## 2. Key Technical Decisions & Rationale

| # | Decision | Why |
|---|---|---|
| **D1** | **Standardized uniform questions per level ($k$) instead of ad-hoc quotas.** | Standardized assessments require uniform difficulty distribution per skill to evaluate competency progression without skewing results. |
| **D2** | **Resilient AI fallback via `auto_generate_missing`.** | Prevents generation failure when Question Bank has fewer questions than $k$ for specific (skill, level) cells by generating from uploaded course documents on-the-fly. |
| **D3** | **Class-scoped question isolation.** | Blueprint sampling strictly filters `BankQuestion.class_id == class_id` to prevent cross-class question contamination. |
| **D4** | **Server-side answer key suppression when `show_solutions_after_submission = false`.** | Frontend-only hiding allows students to inspect answer keys via browser DevTools (Network tab). Sanitizing `is_correct = False`, `explanation = None`, and `rubric = None` on the server guarantees complete exam integrity. |
| **D5** | **Decoupled question swap & regenerator callback.** | `quiz_generation_service.set_regenerator` registers `swap_or_regenerate_question` into the AI-151 question editing workflow without circular dependency. |
| **D6** | **Dreyfus prerequisite gating.** | If a student fails a lower-tier prerequisite (e.g. `BEGINNER`), higher-tier passes (e.g. `COMPETENT`) trigger a `foundation_gap_alert` to prevent guessing anomalies. |
| **D7** | **Alembic migration sequence (`0045_quiz_show_solutions.py`).** | Cleanly chains onto `0044_merge_qbank_quiz_heads` to maintain a single linear migration head following the merge of AI-154. |

---

## 3. Data Models & Schema Specifications

### 3.1 Database Migration (`alembic/versions/0045_quiz_show_solutions.py`)
```sql
ALTER TABLE quiz 
ADD COLUMN show_solutions_after_submission BOOLEAN NOT NULL DEFAULT TRUE;
```

### 3.2 Pydantic DTOs (`app/schemas/quiz.py`)

#### Availability Check
```python
class BlueprintCellAvailability(BaseModel):
    skill_id: uuid.UUID
    skill_code: str
    skill_name: str
    difficulty: BloomLevel
    available_in_bank: int

class BlueprintAvailabilityResponse(BaseModel):
    class_id: uuid.UUID
    cells: list[BlueprintCellAvailability]
    total_bank_questions: int
```

#### Blueprint Generation Request
```python
class BlueprintSkillCell(BaseModel):
    skill_id: uuid.UUID
    difficulty: BloomLevel
    count: int = Field(default=3, ge=1)

class QuizGenerateBlueprintRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = None
    time_limit_minutes: int | None = Field(default=None, gt=0)
    passing_score: float | None = Field(default=None, ge=0)
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    max_attempts: int | None = Field(default=1, gt=0)
    target_skill_ids: list[uuid.UUID] = Field(min_length=1)
    target_levels: list[BloomLevel] = Field(
        default=["NOVICE", "BEGINNER", "COMPETENT", "PROFICIENT", "MASTER"]
    )
    questions_per_level: int = Field(default=3, ge=1, le=20)
    custom_distribution: list[BlueprintSkillCell] | None = None
    auto_generate_missing: bool = True
    document_ids: list[uuid.UUID] = Field(default_factory=list)
    shuffle_questions: bool = True
    shuffle_options: bool = True
    show_solutions_after_submission: bool = True
```

#### Skill Radar Result
```python
class SkillMasteryLevelBreakdown(BaseModel):
    count: int
    earned: float
    max: float
    rate: float | None = None
    passed: bool
    threshold: int
    status: str

class SkillMasteryRadarItem(BaseModel):
    skill_id: str
    skill_code: str
    skill_name: str
    achieved_level: str
    ceiling_level: str | None = None
    percentage: float
    is_blocked: bool = False
    blocked_at: str | None = None
    reason: str | None = None
    foundation_gaps: list[str] = Field(default_factory=list)
    breakdown: dict[str, SkillMasteryLevelBreakdown] = Field(default_factory=dict)
```

---

## 4. API Endpoints Contract

### 4.1 Get Blueprint Matrix Availability
```http
GET /api/classes/{class_id}/quizzes/blueprint-matrix
Authorization: Bearer <trainer_token>
```
**Response (200 OK):**
```json
{
  "class_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "total_bank_questions": 42,
  "cells": [
    {
      "skill_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
      "skill_code": "OOP-01",
      "skill_name": "Object-Oriented Programming",
      "difficulty": "NOVICE",
      "available_in_bank": 6
    },
    {
      "skill_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
      "skill_code": "OOP-01",
      "skill_name": "Object-Oriented Programming",
      "difficulty": "BEGINNER",
      "available_in_bank": 3
    }
  ]
}
```

### 4.2 Generate Quiz from Blueprint
```http
POST /api/classes/{class_id}/quizzes/generate-blueprint
Authorization: Bearer <trainer_token>
Content-Type: application/json

{
  "title": "Software Architecture Midterm Exam",
  "description": "Standardized Dreyfus competency assessment",
  "time_limit_minutes": 60,
  "passing_score": 50.0,
  "target_skill_ids": ["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"],
  "target_levels": ["NOVICE", "BEGINNER", "COMPETENT"],
  "questions_per_level": 3,
  "auto_generate_missing": true,
  "shuffle_questions": true,
  "shuffle_options": true,
  "show_solutions_after_submission": true
}
```
**Response (200 OK):** Returns `QuizTeacherRead` DTO in `DRAFT` status with sampled questions.

---

## 5. Security & Verification

1. **Authorization:** All blueprint generation and swapping endpoints are protected by `_require_class_teacher`.
2. **Answer Concealment:** If `show_solutions_after_submission` is `false`, `quiz_attempt_service._result()` blanks out:
   - `question.explanation = None`
   - `question.rubric = None`
   - `option.is_correct = False`
3. **Verification Suite:**
   - `tests/quizzes/test_quiz_generator.py`: 5/5 passed.
   - `tests/quizzes/`: 83/83 passed.
   - `tests/test_alembic_revision_ids.py`: passed.
   - `ruff check .`: 0 errors.

