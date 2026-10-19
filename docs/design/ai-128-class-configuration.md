# AI-128 — Class Configuration & Roster Capacity

> **Ticket:** AI-128 (parent AI-23) · **Status:** Implemented (backend)
> **Branch:** `features/Design_ClassManagement` · **Companion UI design:**
> `course-qa-agent-fe/docs/design/ai-128-class-configuration.md`

## Purpose

Every class carries its own teaching configuration — delivery mode, default
language, answer scope, citation rule, answer length and session length — plus an
optional roster capacity. A trainer can copy the teaching configuration from
another class they teach, so a run of classes does not need the settings typed in
again. The values are typed and constrained; nothing here is free-form text.

## Scope and confirmed team decisions

- Exactly one configuration row per class (1:1), table `class_config`
  (migration `0009_class_config`).
- The surface is staff-only: the primary trainer, any co-trainer, or an admin.
  Students never reach it (`403` through `_require_class_teacher`; unknown class
  `404`).
- Capacity is optional. Empty (`null`) means unlimited; otherwise it is an integer
  in `1..500` and can never drop below the current active roster.
- Once the roster is full, the join flow refuses new members with `400`
  (`check_capacity`, shared with the enrollment flow).
- `copy` brings the teaching fields only — capacity is deliberately excluded
  because it depends on the target class' own roster.
- Copy sources are the classes the caller teaches (an admin: any class), archived
  classes are never offered, and copying from a class the caller does not teach is
  rejected with `400` (the frontend also scopes its picker through
  `GET /api/classes/config-sources`).
- Invalid vocabulary or ranges fail at the schema layer with `422`.

## API design

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /api/classes/config-sources` | Any staff | Classes the caller may copy from — never archived, never a class the caller does not teach (admins see all classes). Returns `[{id, code, name}]`. |
| `GET /api/classes/{class_id}/config` | Admin / class trainer | Read the configuration. |
| `PATCH /api/classes/{class_id}/config` | Admin / class trainer | Partial update — omitted fields keep their value. |
| `POST /api/classes/{class_id}/config/copy` | Admin / class trainer | Body `{"source_class_id": "..."}`; copies the teaching fields, never capacity. |

`ClassConfigRead` contract:

```json
{
  "capacity": 40,
  "delivery_mode": "hybrid",
  "default_language": "en",
  "answer_scope": "syllabus_first",
  "citation_required": true,
  "max_answer_words": 250,
  "session_length_minutes": 90
}
```

- `capacity`: `null | 1..500` — `null` means unlimited.
- `delivery_mode`: `on_campus | online | hybrid`.
- `default_language`: `en | vi`.
- `answer_scope`: `syllabus_only | syllabus_first | open`.
- `citation_required`: boolean.
- `max_answer_words`: `50..1000` (default 250).
- `session_length_minutes`: `30..180` (default 90).

Domain errors surfaced as `400` with a readable message:

- Setting capacity below the current roster:
  `"Capacity cannot be lower than the current roster (N)."`
- Joining a full class (shared with enrollment):
  `"Class 'CS101-A' is full: 40/40 students. Raise the capacity in Course
  Settings to accept more."`
- Copy source not taught by the caller: `400`. Copy source archived: `400`.

## Backend responsibilities

- `app/models/course.py` — `ClassConfig` model (1:1 with `Class`).
- `alembic/versions/0009_class_config.py` — the configuration table.
- `app/schemas/admin.py` — `ClassConfigRead`, `ClassConfigUpdate`,
  `ClassConfigCopy`, `ClassConfigSourceRead` (typed literals enforce the
  vocabulary; out-of-range values fail with `422`).
- `app/services/course_service.py`
  - `get_config` / `update_config` — row-per-class access with the capacity
    guardrails (`MIN_CAPACITY`, `MAX_CAPACITY`).
  - `copy_config(db, target_id, source_id, allowed_source_ids)` — teaching fields
    only; refuses archived sources and sources outside the caller's scope.
  - `check_capacity(db, class_id)` — used by the enrollment flow.
- `app/routers/class_config.py` — the four routes above, registered in
  `app/main.py`.

## Edge cases

- Patching only `{"capacity": 0}` → `422` (schema bound) while `"capacity": null`
  is a valid "unlimited" value.
- A copy from a class in another semester is allowed; the configuration is
  content, not schedule.
- Capacity counts only active students; teaching assistants do not count against
  it.

## Test coverage

- `tests/test_class_config.py` (12 tests): role matrix (`403` for students,
  `404` unknown class), typed vocab (`422`), partial updates, capacity floor vs
  roster (`400`), full-roster join refusal, copy excludes capacity, archived
  sources rejected, config-sources scoping.

Run from the backend repository root:

```powershell
uv run python -m pytest tests/test_class_config.py -q
uv run ruff check .
```

## Acceptance-criteria mapping

| Jira criterion | Agreed implementation |
|---|---|
| AC-01: Trainer/co-trainer/admin read & update; everyone else rejected | `_require_class_teacher` on every config route; students never see the surface. |
| AC-02: Values constrained, invalid → 422 | Typed literals and numeric bounds in `ClassConfigUpdate`; FastAPI answers `422`. |
| AC-03: Capacity optional 1..500, never below roster, full roster refuses joins | `null` = unlimited; `update_config` compares against the active roster (`400`); `check_capacity` blocks joins (`400`). |
| AC-04: Partial update semantics | `model_dump(exclude_unset=True)` — omitted keys are untouched. |
| AC-05: Copy from another class; never capacity, never archived, scope enforced | `POST .../config/copy` + `allowed_source_ids` in `copy_config`; `config-sources` drives the pickup list. |

## Branch and merge-request convention

Keep the design and implementation deliverables separate:

- Design branch: `features/Design_ClassManagement`; contains this document and its
  siblings for AI-127, AI-129 and AI-130 under `docs/design/`.
- Implementation branch: `features/Implementation_ClassManagement`; contains the
  backend code, migrations and tests, but not these design documents.

Create separate merge requests for the two branches and link both to AI-127..131.
Do not mark review, CI, or manual-test checklist items complete without evidence.

## Out of scope

- Consuming the stored teaching configuration inside the RAG/chat pipeline in new
  ways — this story stores and exposes it; existing consumers keep their current
  behaviour.
- Capacity overrides per semester or waitlists once a class is full.
- Per-student or per-group configuration.
