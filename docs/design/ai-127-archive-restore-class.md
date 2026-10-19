# AI-127 — Archive & Restore a Class

> **Ticket:** AI-127 (parent AI-23) · **Status:** Implemented (backend)
> **Branch:** `features/Design_ClassManagement` · **Companion UI design:**
> `course-qa-agent-fe/docs/design/ai-127-class-archive-restore.md`

## Purpose

A finished class should stop cluttering the active catalog without anything being
deleted. An admin archives a class; its trainer and students no longer see it;
the admin can restore it later with the roster, documents, chunks and embeddings
untouched. Permanent deletion stays available only for classes that are already
archived.

## Scope and confirmed team decisions

- The class lifecycle uses the existing `class.status` column
  (`ACTIVE` | `ARCHIVED`). No new table; no data migration.
- Archiving is soft by definition: the class row, its accounts, roster, documents,
  chunks and Qdrant vectors are all retained.
- Lifecycle changes go through one admin-only endpoint,
  `PATCH /api/admin/classes/{id}/status`; there is no per-purpose archive route.
- Archived classes leave every workspace list: the trainer class list
  (`class_ids_for_trainer(..., active_only=True)`), the student class list, and the
  class-scoped document catalog (`document_service` builds its allowed set from
  active-only classes).
- The student surface answers `404` for archived classes to anyone who is not an
  admin (member guard in `app/routers/student/router.py`).
- Hard delete (`DELETE /api/admin/classes/{id}`) is guarded by `course_service`:
  only an archived class can be permanently deleted.
- Only an admin can archive, restore or delete — `require_admin` on every route.

## API design

| Method and path | Auth | Purpose |
|---|---|---|
| `PATCH /api/admin/classes/{class_id}/status` | Admin | Body `{"status": "ARCHIVED"}` archives; `{"status": "ACTIVE"}` restores. Returns the updated `ClassRead`. |
| `GET /api/admin/classes` | Admin | Returns every class including archived ones; each `ClassRead` carries `status` so the UI can badge and filter. |
| `DELETE /api/admin/classes/{class_id}` | Admin | Permanent delete; `DomainError` (not archived / unknown) maps to `404`, otherwise `{"deleted": id, ...warnings}`. |

`ClassRead` additions used by this story:

```json
{
  "id": "...",
  "code": "CS101-FA26-002",
  "status": "ACTIVE",
  "capacity": 40,
  "student_count": 12
}
```

- Unknown class id on the status route → `DomainError` → `400`; unknown id on
  delete → `404`.
- Restoring sets `status = ACTIVE` and nothing else changes.

## Backend responsibilities

- `app/services/course_service.py`
  - `STATUS_ACTIVE`, `STATUS_ARCHIVED`, `CLASS_STATUSES` constants and the comment
    documenting the "archive, never delete" lifecycle.
  - `set_class_status(db, class_id, status)` — validates the target status and
    flips the row.
  - `list_classes(..., active_only=...)` variants: archived classes are dropped
    for trainer and student workspaces.
  - `delete_class(db, class_id)` — refuses unless the class is archived.
- `app/routers/admin/router.py` — `PATCH /classes/{id}/status` behind
  `require_admin`, returning `_class_to_read`.
- `app/routers/student/router.py` — `_require_member` treats archived classes as
  missing (`404`) for non-admin callers.
- `app/services/document_service.py` — builds the visible document set from active
  classes only, so archived classes' documents disappear from the catalog.

## Edge cases

- Archiving an already archived class is a no-op status write; restoring an active
  class likewise.
- An archived class keeps its primary trainer assignment; it simply stops
  appearing in the trainer workspace.
- Tickets, roster rows and outbox rows are untouched by archive and restore.

## Test coverage

- `tests/test_class_config.py`
  - `test_archived_class_leaves_the_workspace_and_can_be_restored` — archive hides
    the class from trainer/student lists, documents leave the catalog, restore
    brings everything back.
  - `test_admin_sees_capacity_and_status_on_the_class_list` — the admin list
    carries `status`/`capacity` for the badge and column.

Run from the backend repository root:

```powershell
uv run python -m pytest tests/test_class_config.py -q
uv run ruff check .
```

## Acceptance-criteria mapping

| Jira criterion | Agreed implementation |
|---|---|
| AC-01: Archive from the class management screen with confirmation | `PATCH .../status {status: ARCHIVED}` behind `require_admin`; the confirmation dialog lives in the UI (see companion FE design). |
| AC-02: Archived class and documents disappear from active lists | Active-only filtering in `course_service` (trainer/student lists) and `document_service` (catalog); the admin list keeps the row with an `ARCHIVED` status. |
| AC-03: Nothing is deleted | Archive only writes `status`; roster, accounts, files, chunks and vectors are retained. |
| AC-04: Students reach archived material through a read-only section | **Not delivered here** — tracked by AI-126 (open). Non-admin students get `404` for archived classes for now. |
| AC-05: Trainer of an archived class cannot change documents | Follows from the archived class leaving the trainer workspace (list + document set), so the upload/replace/delete flows no longer offer the class. |
| AC-06: Restore returns the class unchanged | `{"status": "ACTIVE"}` restores with the same accounts, documents, chunks and embeddings. |
| AC-07: Admin only | `require_admin` on status and delete routes. |
| AC-08: Archived badge and default exclusion with filter | `status` is exposed on `ClassRead`; the badge/filter is a UI concern (companion FE design). |

## Branch and merge-request convention

Keep the design and implementation deliverables separate:

- Design branch: `features/Design_ClassManagement`; contains this document and its
  siblings for AI-128, AI-129 and AI-130 under `docs/design/`.
- Implementation branch: `features/Implementation_ClassManagement`; contains the
  backend code, migrations and tests, but not these design documents.

Create separate merge requests for the two branches and link both to AI-127..131.
Do not mark review, CI, or manual-test checklist items complete without evidence.

## Out of scope

- Read-only "Archived classes" section for students (AI-126, open story).
- Scheduled/automatic archiving and bulk archive operations.
- Restoring a class into a different semester; restore keeps every field as-is.
- Any metrics or audit trail for lifecycle changes beyond the status column.
