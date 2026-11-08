# AI-159 — Class-Scoped Skill Taxonomy & 5-Level Proficiency Rubrics Engine

> **Ticket:** [AI-159](https://ai03.atlassian.net/browse/AI-159) (Epic AI-147: Quiz Generation & Assessment) | **Priority:** High | **Story Points:** 5 SP  
> **Target Branch:** `features/Implementation_SkillTaxonomyRubrics`  
> **Design Branch:** `features/Design_SkillTaxonomyRubrics`  
> **Frontend Companion:** `course-qa-agent-fe/docs/design/ai-159-skill-taxonomy-rubrics-ui.md`  
> **Related Stories:** AI-160 (Question Bank & Misconception Mining), AI-148/AI-149/AI-151 (Quiz generation, blueprint assembly & review).

---

## 1. Purpose and Scope

### 1.1 Problem Statement
In traditional assessment generation, LLMs generate quiz questions directly from loose text documents without a coherent competency anchor. This produces two critical failure modes:
1. **Taxonomy Drift & Redundancy:** Questions test arbitrary superficial trivia rather than measurable learning outcomes.
2. **Inconsistent Difficulty Grading:** "Easy", "Medium", and "Hard" labels lack formal pedagogical definitions and fail to assess cognitive progression.

AI-159 establishes the **foundational competency layer**: a class-scoped, deterministic Skill Taxonomy paired with 5-level proficiency rubrics based on the **Dreyfus Model of Skill Acquisition** (`NOVICE` $\rightarrow$ `MASTER`). All downstream question generation (AI-160) and quiz blueprint matrices (AI-149/AI-151) bind directly to this taxonomy.

### 1.2 Acceptance Criteria (Jira AI-159)
* **AC-01:** Trainer or admin can trigger automatic AI synthesis of 3–8 core skills anchored to class metadata (`name`, `code`, `credits`, `description`, `notes`).
* **AC-02:** Each generated skill contains a unique normalized uppercase snake_case identifier code per class (`class_id` + `code`), a display name, high-level category grouping, and an overview description.
* **AC-03:** Each skill automatically creates exactly 5 progressive Dreyfus proficiency rubrics: `NOVICE` (80% pass threshold), `BEGINNER` (75%), `COMPETENT` (70%), `PROFICIENT` (65%), and `MASTER` (60%).
* **AC-04:** Each rubric level specifies clear, actionable evaluation scope descriptions detailing required student comprehension and depth of understanding.
* **AC-05:** Trainers can view, edit rubric descriptions and pass thresholds, or manually create/update/delete skills scoped to their class.
* **AC-06:** Enforce database integrity with foreign keys, cascade deletion, and composite constraints preventing cross-class contamination between skills and classes.

### 1.3 Out of Scope
* Question generation and document chunk extraction (deferred to AI-160).
* Student adaptive testing and quiz attempt grading (handled in AI-153/AI-154).

---

## 2. Pedagogical Architecture & The Dreyfus Model

Rather than arbitrary difficulty tags, each skill is defined along 5 developmental stages:

| Level | Dreyfus Stage | Cognitive Focus | Default Pass Threshold | Question Characteristics |
|---|---|---|:---:|---|
| **NOVICE** | Rule-based Recall | Recognize facts, syntax, definitions, standard formulas | **80%** | Single-concept direct questions, minimal distractor trap |
| **BEGINNER** | Contextual Understanding | Apply standard procedures, identify obvious pitfalls | **75%** | Common scenarios, routine troubleshooting |
| **COMPETENT** | Analytical Application | Prioritize approaches, integrate 2+ concepts, optimize | **70%** | Multi-step problem solving, trade-off analysis |
| **PROFICIENT** | Holistic Synthesis | Edge case detection, deep architectural analysis, debugging | **65%** | Complex system behaviors, ambiguous scenarios |
| **MASTER** | Intuitive & Meta Mastery | Novel edge case resolution, structural refactoring, system design | **60%** | Intricate multi-component failure analysis |

> **Threshold Rationale:** Lower percentage thresholds at higher levels reflect increasing cognitive load and ambiguity. Achieving 60% on complex master-level diagnostic challenges represents solid mastery, whereas 80% is required on basic novice recall to verify core foundational literacy.

---

## 3. Data Model & Database Architecture

```mermaid
erDiagram
    CLASS ||--o{ SKILL : "contains (scoped)"
    SKILL ||--|{ SKILL_LEVEL_RUBRIC : "defines 5 levels"
    SKILL ||--o{ QUIZ_QUESTION : "mapped to (AI-160)"

    SKILL {
        uuid id PK
        uuid class_id FK "CASCADE"
        string code "UPPERCASE_SNAKE_CASE"
        string name
        string category
        text description
        datetime created_at
        datetime updated_at
    }

    SKILL_LEVEL_RUBRIC {
        uuid id PK
        uuid skill_id FK "CASCADE"
        string level "NOVICE | BEGINNER | COMPETENT | PROFICIENT | MASTER"
        text description "Evaluation criteria scope"
        integer threshold_pct "Pass threshold percentage"
    }
```

### 3.1 Constraints & Indexes
1. **Uniqueness:** Composite unique constraint `(class_id, code)` prevents duplicate skill codes within the same class while allowing different classes to reuse standard names.
2. **Rubric Level Uniqueness:** Composite unique constraint `(skill_id, level)` ensures exactly one rubric record per proficiency tier per skill.
3. **Foreign Key Integrity:** `ondelete="CASCADE"` on `Skill.class_id` and `SkillLevelRubric.skill_id` guarantees clean lifecycle cleanup when a class or skill is removed.

---

## 4. API Specification & HTTP Contract

All routes are mounted under `/api/classes/{class_id}/skills` and protected by `_require_class_teacher`.

### 4.1 Endpoints Summary

| Method | Endpoint | Description | Status Code |
|---|---|---|:---:|
| `GET` | `/api/classes/{class_id}/skills` | List all skills and level rubrics for the class | `200 OK` |
| `GET` | `/api/classes/{class_id}/skills/{skill_id}` | Retrieve skill details with rubrics | `200 OK` |
| `POST` | `/api/classes/{class_id}/skills` | Manually create a skill (bootstraps 5 levels if omitted) | `201 Created` |
| `PATCH` | `/api/classes/{class_id}/skills/{skill_id}` | Update skill metadata (name, description, category) | `200 OK` |
| `DELETE` | `/api/classes/{class_id}/skills/{skill_id}` | Delete skill and cascade remove rubrics | `204 No Content` |
| `PUT` | `/api/classes/{class_id}/skills/{skill_id}/rubrics/{level}` | Update evaluation description and pass threshold | `200 OK` |
| `POST` | `/api/classes/{class_id}/skills/generate` | Auto-generate skills & rubrics from class metadata via LLM | `201 Created` |

### 4.2 Auto-Generation Contract (`POST .../skills/generate`)
* **Request:** Empty body (relies on path `{class_id}`).
* **Authorization:** Admin or Trainer assigned to the class.
* **Response:** Array of `SkillRead` with pre-populated 5-level rubrics.
* **Idempotency & Merging:** If a skill with the generated code already exists, its rubrics and description are updated/supplemented rather than duplicated.

---

## 5. Single-Shot AI Generation Engine

To maintain high responsiveness, minimize operational cost, and completely avoid LLM rate limit blocks (429), skill generation uses a **Single-Shot Batch Architecture**:

1. **Metadata Aggregation:** Combines `class.name`, `class.code`, `class.credits`, `class.semester`, `class.academic_year`, and `class_info.description / note`.
2. **Prompt Manager:** Template configured in [`app/config/prompts/class_skill_taxonomy.yaml`](file:///c:/Users/nghia/OneDrive/Desktop/FAcaProject/course-qa-agent-be/app/config/prompts/class_skill_taxonomy.yaml) with explicit JSON schema output instructions.
3. **Deterministic Normalization:** All AI-produced codes are sanitized through `normalize_skill_code()`:
   $$\text{normalize}(s) = \text{re.sub}(r'[^A-Z0-9_]+', '\_', s.upper()).strip('\_')$$
4. **Transaction Boundary:** All 4–8 skills and their respective 20–40 rubric levels are persisted inside a single atomic database transaction (`db.commit()`).

---

## 6. Security & Multi-Tenancy

* **Strict Role-Based Scoping:** Handled by `_require_class_teacher(db, user, class_id)`.
  - Admins: Unrestricted access across all classes.
  - Trainers: Permitted only if `class_id in course_service.class_ids_for_trainer(db, user.id)`.
  - Students / Foreign Trainers: Blocked with `403 Forbidden`.
* **Cross-Class Isolation:** All queries explicitly filter by `Skill.class_id == class_id`.

---

## 7. Testing Strategy

1. **Unit Tests (`tests/test_skill_taxonomy_service.py`):**
   - Code normalization edge cases (hyphens, spaces, accents, lowercase).
   - Dreyfus pass/fail evaluation engine across boundary scores.
   - Comprehensive multi-skill student mastery calculation.
2. **Migration & DB Tests (`tests/test_alembic_revision_ids.py`):**
   - Verification that migration ID `0041_skill_taxonomy` conforms to `alembic_version` VARCHAR length.
   - Foreign key cascading verification.

