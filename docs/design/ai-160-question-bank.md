# AI-160 — Question Bank Generation & Repository Management Engine

> **Ticket:** [AI-160](https://ai03.atlassian.net/browse/AI-160) (Epic AI-147: Quiz Generation & Assessment) | **Priority:** High | **Story Points:** 5 SP  
> **Target Branch:** `features/Implementation_QuestionBank`  
> **Backend Implementation:** `course-qa-agent-be`  
> **Frontend Companion:** `course-qa-agent-fe/docs/design/ai-160-question-bank-ui.md`  
> **Related Stories:** AI-159 (Skill Taxonomy & 5-Level Rubrics), AI-148/AI-149/AI-151 (Quiz generation, blueprint assembly & review).

---

## 1. Purpose and Scope

### 1.1 Problem Statement
Standard assessment systems frequently rely on ad-hoc question creation or ungrounded generative prompts. This leads to two critical shortcomings:
1. **Taxonomy Disconnection:** Questions fail to assess defined learning outcomes or map to concrete cognitive stages.
2. **Ignored Student Friction Points:** Real-world learning obstacles and recurring student misconceptions from Q&A chats and support tickets are never systematically integrated into formative diagnostics.

AI-160 provides a centralized **Question Bank repository and multi-modal generation engine**:
* Direct binding of every question to a class-scoped competency (`skill_id`) and Dreyfus proficiency tier (`NOVICE` $\rightarrow$ `MASTER`).
* Grounded chunk extraction and question synthesis from class documents.
* Diagnostic question mining that actively analyzes student questions and support tickets to test and reinforce tricky concepts.

### 1.2 Acceptance Criteria (Jira AI-160)
* **AC-01:** Questions in the bank are tagged with `class_id`, optional `skill_id`, and difficulty level (`NOVICE`, `BEGINNER`, `COMPETENT`, `PROFICIENT`, `MASTER`).
* **AC-02:** Document-grounded AI generator extracts content chunks from class documents and produces structured questions with answer options, correct flags, and source citations.
* **AC-03:** AI generation supports per-level quotas ($k$ questions per selected Dreyfus tier) rather than arbitrary distribution.
* **AC-04:** Misconception mining engine queries student chat messages and support tickets (`Ticket.question`) over a configurable timeframe (e.g. past 7–30 days) to generate targeted probe questions addressing common confusion points.
* **AC-05:** Supports multiple question formats: `SINGLE_CHOICE`, `MULTIPLE_CHOICE`, and `FILL_IN_BLANK`.
* **AC-06:** Teachers can search, filter (by skill, type, difficulty), create, update, and delete questions with full class isolation and cascade deletion.
* **AC-07:** Database schema and migrations are idempotent, preserving quiz availability schema compatibility (`0040_quiz_availability`).

---

## 2. Architecture & Data Model

```mermaid
erDiagram
    CLASS ||--o{ BANK_QUESTION : "owns (scoped)"
    SKILL ||--o{ BANK_QUESTION : "mapped to (optional)"
    BANK_QUESTION ||--|{ BANK_QUESTION_OPTION : "has options"

    BANK_QUESTION {
        uuid id PK
        uuid class_id FK "CASCADE"
        uuid skill_id FK "SET NULL"
        string question_type "SINGLE_CHOICE | MULTIPLE_CHOICE | FILL_IN_BLANK"
        text question "Question stem"
        float points "Default 1.0"
        string difficulty "NOVICE | BEGINNER | COMPETENT | PROFICIENT | MASTER"
        text explanation "Diagnostic explanation"
        text rubric "Evaluation rubric"
        string source_type "DOCUMENT | CHAT_MISCONCEPTION | MANUAL"
        uuid source_document_id FK "SET NULL"
        string source_reference "Chunk / Page citation"
        string status "APPROVED | DRAFT | ARCHIVED"
        json grading_config "Grading configuration"
        datetime created_at
        datetime updated_at
    }

    BANK_QUESTION_OPTION {
        uuid id PK
        uuid question_id FK "CASCADE"
        string option_label "A, B, C, D..."
        text option_text "Option description"
        boolean is_correct "True for correct answers"
        integer order_index "Sort order"
    }
```

### 2.1 Database Tables & Constraints
* **`bank_question` Table:**
  * Primary key `id` (UUID).
  * Foreign key `class_id` references `class.id` with `ondelete="CASCADE"`.
  * Foreign key `skill_id` references `skill.id` with `ondelete="SET NULL"`.
  * Check constraint `ck_bank_question_difficulty`: enforces Dreyfus tiers (`NOVICE`, `BEGINNER`, `COMPETENT`, `PROFICIENT`, `MASTER`) while maintaining backward compatibility with Bloom levels (`REMEMBER`, `UNDERSTAND`, `APPLY`).
  * Check constraint `ck_bank_question_type`: enforces `SINGLE_CHOICE`, `MULTIPLE_CHOICE`, `FILL_IN_BLANK`.
  * Index on `(class_id, skill_id)` and `(class_id, difficulty)` for high-performance filtering.
* **`bank_question_option` Table:**
  * Foreign key `question_id` references `bank_question.id` with `ondelete="CASCADE"`.
  * Sorted via `order_index`.

---

## 3. AI Generation Engines

### 3.1 Document-Grounded Question Synthesis (`generate_from_documents`)
1. Reads content chunks from selected `Document` records scoped to the class.
2. Loads YAML prompt configuration from `app/config/prompts/question_bank_generation.yaml`.
3. Distributes generation according to the teacher's requested `questions_per_level` dictionary:
   $$\text{Total Questions} = \sum_{\text{level}} \text{count}_{\text{level}}$$
4. Synthesizes options, identifies correct answers, generates pedagogical explanations, and sets `source_type = "DOCUMENT"`.

### 3.2 Student Doubts & Misconception Mining (`mine_misconceptions`)
1. Queries `ChatMessage` records (student role) and `Ticket` records (`Ticket.question` and `Ticket.agent_answer`) created within the last $N$ days (default 14 days).
2. Synthesizes student confusion points, friction keywords, and misunderstanding patterns.
3. Loads YAML prompt configuration from `app/config/prompts/misconception_probing.yaml`.
4. Produces diagnostic probe questions specifically structured to expose and clarify identified misconceptions, setting `source_type = "CHAT_MISCONCEPTION"`.

---

## 4. API Specification & HTTP Contract

All endpoints are mounted under `/api/classes/{class_id}/question-bank` and guarded by `_require_class_teacher`.

| Method | Endpoint | Description | Status Code |
|---|---|---|:---:|
| `GET` | `/api/classes/{class_id}/question-bank` | List questions with search, skill, type, and difficulty filters | `200 OK` |
| `POST` | `/api/classes/{class_id}/question-bank` | Manually add a new question with options | `201 Created` |
| `GET` | `/api/classes/{class_id}/question-bank/{question_id}` | Retrieve question details and options | `200 OK` |
| `PUT` | `/api/classes/{class_id}/question-bank/{question_id}` | Update question stem, options, skill, or difficulty | `200 OK` |
| `DELETE` | `/api/classes/{class_id}/question-bank/{question_id}` | Delete a question (cascades options) | `204 No Content` |
| `POST` | `/api/classes/{class_id}/question-bank/generate-from-documents` | Trigger AI generation from class documents | `200 OK` |
| `POST` | `/api/classes/{class_id}/question-bank/mine-misconceptions` | Trigger diagnostic generation from chat logs & tickets | `200 OK` |
| `POST` | `/api/classes/{class_id}/question-bank/bulk-status` | Batch update status (`APPROVED`, `ARCHIVED`) | `200 OK` |

---

## 5. Migration Strategy

* Migration file: `alembic/versions/0042_question_bank.py`
* Down revision: `0041_skill_taxonomy`
* Includes idempotent column inspection safeguards ensuring `opens_at`, `closes_at`, and `max_attempts` on `quiz` are present, preventing migration drift across existing databases.

---

## 6. Verification and Test Strategy

* **Unit Tests (`tests/test_question_bank.py`):**
  * Verifies Question Bank CRUD operations and relationship cascade.
  * Verifies document chunk question generation with per-level quotas.
  * Verifies student misconception mining from chat logs and support tickets.
* **Schema Verification (`tests/test_quiz_schema.py`):**
  * Validates Dreyfus level compatibility in Pydantic schemas and models.
  * Validates Alembic migration upgrade and downgrade cycle.
* **Full Backend Test Suite:** 813 tests passing with 0 failures (`pytest -q`).
* **Linter:** `ruff check .` passing cleanly with 0 lint errors.

