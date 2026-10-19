# AI-130 — Co-teachers per Class

> **Ticket:** AI-130 (parent AI-23) · **Status:** Implemented (backend)
> **Branch:** `features/Design_ClassManagement` · **Companion UI design:**
> `course-qa-agent-fe/docs/design/ai-130-class-co-teachers.md`

## Purpose

A class keeps its primary trainer while also carrying one or more co-trainers, so
co-teaching or a mid-semester handover does not require recreating the class or
its members. A co-trainer sees the class in their workspace exactly like the
primary trainer.

## Scope and confirmed team decisions

- `class.trainer_id` remains the **primary** trainer; extra trainers live in the
  dedicated `class_trainer` table (migration `0008_class_trainer`), unique per
  `(class_id, trainer_id)` pair.
- This story deliberately extends **AI-31 AC-02** ("a class has exactly one
  assigned trainer"): the primary trainer model is unchanged, co-teachers are
  additive.
- A co-trainer counts as "teaching this class" everywhere the trainer workspace is
  computed (`class_ids_for_trainer`): class list, class details, class members,
  class documents, and the staff write-gate used by the configuration and
  information surfaces.
- The primary trainer cannot be removed through the co-trainer route — changing
  the owner stays on `PATCH /api/admin/classes/{id}/assign`. The co-trainer route
  answers `400` for that attempt.
- Adding the same trainer twice (or adding the primary trainer again) answers
  `400`.
- Admin class list responses expose the full trainer set with the primary first
  and an `is_primary` flag.

## API design

| Method and path | Auth | Purpose |
|---|---|---|
| `POST /api/admin/classes/{class_id}/trainers` | Admin | Body `{"trainer_id": "..."}` — add a co-trainer. Returns the updated `ClassRead`. |
| `DELETE /api/admin/classes/{class_id}/trainers/{trainer_id}` | Admin | Remove a co-trainer. Primary trainer → `400`. Returns the updated `ClassRead`. |
| `PATCH /api/admin/classes/{class_id}/assign` | Admin | Unchanged: replace the primary trainer. |

`ClassRead.trainers` contract (primary first):

```json
{
  "id": "...",
  "trainer_id": "...",
  "trainer_name": "Trung Nguyen",
  "trainers": [
    {"trainer_id": "...", "name": "Trung Nguyen", "email": "...", "is_primary": true},
    {"trainer_id": "...", "name": "Mai Pham",    "email": "...", "is_primary": false}
  ]
}
```

The legacy `trainer_id` / `trainer_name` fields stay for existing consumers; the
`trainers[]` array is authoritative for display.

## Backend responsibilities

- `app/models/course.py` — `ClassTrainer` model + relationship on `Class`.
- `alembic/versions/0008_class_trainer.py` — the join table with a unique
  `(class_id, trainer_id)` constraint and cascade on class delete.
- `app/schemas/admin.py` — `ClassTrainerRead` and `ClassRead.trainers`.
- `app/services/course_service.py`
  - `add_trainer` / `remove_trainer` — duplicate and primary-guard rules.
  - `class_ids_for_trainer` — unions primary (`class.trainer_id`) and co-trainer
    rows, so every workspace computation sees co-teachers.
  - `trainers_for_class` — ordered primary-first for `_class_to_read`.
- `app/routers/admin/router.py` — the add/remove routes behind `require_admin`.

## Edge cases

- Removing a co-trainer affects only the co-trainer table; the primary is
  untouched.
- Deleting a class cascades `class_trainer` rows (join table only — no user
  accounts are touched).
- A trainer account must be active and hold the trainer role to be added
  (validated through the service).

## Test coverage

- `tests/test_class_trainers.py` (4 tests): add/remove flow through the admin
  routes, duplicate add `400`, primary removal via the co-trainer route `400`,
  co-trainer parity in the trainer workspace (`class_ids_for_trainer`).

Run from the backend repository root:

```powershell
uv run python -m pytest tests/test_class_trainers.py -q
uv run ruff check .
```

## Acceptance-criteria mapping

| Jira criterion | Agreed implementation |
|---|---|
| AC-01: Admin class list exposes every trainer, primary first, with `is_primary` | `trainers_for_class` orders primary first; `ClassRead.trainers` carries `name`, `email`, `is_primary`. |
| AC-02: Add/remove co-trainer; primary not removable via this route (`400`); duplicate rejected (`400`) | `add_trainer`/`remove_trainer` guards; primary changes stay on `/assign`. |
| AC-03: Co-trainer sees the class like the primary (list, details, members, documents) | `class_ids_for_trainer` unions primary + co-teachers; all workspace queries use it. |
| AC-04: Member list shows every trainer | The class member surface renders `ClassRead.trainers` (companion FE design). |

## Branch and merge-request convention

Keep the design and implementation deliverables separate:

- Design branch: `features/Design_ClassManagement`; contains this document and its
  siblings for AI-127, AI-128 and AI-129 under `docs/design/`.
- Implementation branch: `features/Implementation_ClassManagement`; contains the
  backend code, migrations and tests, but not these design documents.

Create separate merge requests for the two branches and link both to AI-127..131.
Do not mark review, CI, or manual-test checklist items complete without evidence.

## Out of scope

- Teaching assistants as a distinct role on the class — TAs keep their current
  model and do not count against capacity or appear in `trainers[]` as
  co-teachers.
- Permission differences between primary and co-trainers beyond the primary-guard
  rules above; both share the same staff gates.
- Notifying co-trainers when they are added or removed.
