# AI-134 — Class Prerequisites & Enrollment Eligibility

> **Ticket:** AI-134 · **Status:** Implemented (backend)
> **Branch:** `features/Design_ClassPrerequisite` · **Companion UI design:**
> `course-qa-agent-fe/docs/design/ai-134-class-prerequisites.md`

## Purpose

A class can declare other classes as prerequisites. A student may only join a
class once they have **PASSED** every prerequisite; a trainer records the term
result (`PASSED` / `FAILED`) on each student's enrollment in that prerequisite
class. The catalog tells students up front which prerequisites are still
blocking them instead of surfacing the rule only when the join request fails.

## Scope and confirmed team decisions

- Completion is tracked on a new nullable column `enrollment.result`
  (`NULL` = not graded yet, `PASSED`, `FAILED`). **`enrollment.status` is never
  reused or changed** by grading — membership and academic result stay separate.
- Prerequisite links live in `class_prerequisite(class_id,
  prerequisite_class_id)`; a pair is unique and deleting a class cascades its
  links.
- A class cannot be its own prerequisite; duplicates are collapsed; unknown ids
  are rejected.
- **Only a trainer who teaches the class** (primary or co-trainer) may record a
  result. Teaching assistants and admins are rejected with `403` by design.
- The gate is enforced twice: on the student's join request and again on
  approval, so a client cannot bypass it.
- Cross-class prerequisite cycles (A → B → A) are **not detected** in this
  story; only self-reference is blocked. Documented limitation.

## Data model

```text
enrollment.result            VARCHAR(20) NULL, CHECK-free string: PASSED | FAILED | NULL
class_prerequisite
  id                       UUID PK
  class_id                 UUID FK class.id            ON DELETE CASCADE
  prerequisite_class_id    UUID FK class.id            ON DELETE CASCADE
  UNIQUE (class_id, prerequisite_class_id)
```

Migration `0014_class_prerequisite` (down_revision `0013_doc_failure_reason`)
adds the column and the table. Schema is applied automatically by the backend
container entrypoint (`alembic upgrade head` before uvicorn); there is no
`create_all` at startup anymore.

## API design

| Method and path | Auth | Purpose |
|---|---|---|
| `POST /api/admin/classes` | Admin | Create a class; optional `prerequisite_class_ids`. |
| `GET /api/admin/classes` | Admin | Every class carries `prerequisites: [{id, code, name}]`. |
| `PATCH /api/admin/classes/{class_id}/prerequisites` | Admin | Replace the full prerequisite set atomically. |
| `GET /api/student/catalog` | Any signed-in user | Classes plus `blocked_prerequisites` and `can_request` (gated for student/TA). |
| `POST /api/student/classes/{class_id}/request` | Student / TA | Rejects when a prerequisite is missing (`400`). |
| `POST /api/student/classes/{class_id}/requests/{student_id}/approve` | Class TA / trainer / admin | Re-checks prerequisites before activation (`400`). |
| `GET /api/student/classes/{class_id}/members` | Class staff | Member rows now include `result`. |
| `PATCH /api/student/classes/{class_id}/members/{student_id}/result` | Class trainer only | Set `PASSED`, `FAILED` or `null`. |

### Replace prerequisites

```http
PATCH /api/admin/classes/6f0f.../prerequisites
{"prerequisite_class_ids": ["0b8e...", "41c2..."]}
```

```json
{
  "id": "6f0f...",
  "code": "SWE301",
  "prerequisites": [
    {"id": "0b8e...", "code": "SWE101", "name": "Programming Fundamentals"},
    {"id": "41c2...", "code": "SWE201", "name": "Data Structures"}
  ]
}
```

Sending `[]` clears every prerequisite. The endpoint replaces the whole set; it
is not additive.

### Catalog entry

```json
{
  "id": "6f0f...",
  "code": "SWE301",
  "name": "Software Engineering",
  "my_status": "none",
  "blocked_prerequisites": [
    {"id": "0b8e...", "code": "SWE101", "name": "Programming Fundamentals"}
  ],
  "can_request": false
}
```

`blocked_prerequisites` is computed only for `STUDENT` and `TEACHING_ASSISTANT`;
trainers and admins always receive an empty list and `can_request: true`.

### Record a result

```http
PATCH /api/student/classes/0b8e.../members/9ad3.../result
{"result": "PASSED"}
```

`null` clears the mark. Response is the member row with the stored `result`.

## Errors

| Status | When |
|---|---|
| `400` | class is its own prerequisite; unknown prerequisite class; result not in `{null, PASSED, FAILED}`; student not enrolled in the class; join/approve while prerequisites are missing (`Prerequisites not passed: SWE101 — ...`). |
| `403` | non-trainer (TA / admin / unrelated trainer) calls the result endpoint: `Only the class trainer can record results.` |
| `404` | unknown class / unknown pending request where the endpoint looks the class up by id. |
| `422` | payload shape invalid (e.g. `result: "passed"` is accepted by schema as a string but rejected in the service with `400`; non-UUID ids fail validation). |

## Backend responsibilities

- `app/models/course.py` — `Enrollment.result`, `ClassPrerequisite` with the
  unique pair and cascade deletes.
- `alembic/versions/0014_class_prerequisite.py` — the migration above.
- `app/schemas/admin.py` — `ClassPrerequisiteRead`, `ClassPrerequisiteUpdate`,
  `ClassCreate.prerequisite_class_ids`, `ClassRead.prerequisites`.
- `app/services/course_service.py` — `class_prerequisites`,
  `set_class_prerequisites` (validate → delete → insert in one commit),
  `missing_prerequisites` (compares against `result = PASSED`), `check_prerequisites`,
  `set_enrollment_result`.
- `app/routers/admin/router.py` — `_class_to_read` hydrates prerequisites; create
  passes ids through; `PATCH .../prerequisites`.
- `app/routers/student/router.py` — catalog flags, join/approve gates, members
  query joins `Enrollment` for `result`, trainer-only result endpoint.

## Edge cases

- A prerequisite may itself have prerequisites; the gate only checks the direct
  prerequisites of the target class (transitive chains are not enforced).
- Grading a class is independent of membership: a student who is `PENDING` in a
  class can still be graded in another class where they are enrolled.
- Archiving a prerequisite class does not remove the link; the student still
  needs a `PASSED` result recorded there.
- Admin class list shows prerequisites for every class, including archived ones.
- The result endpoint validates the caller against the class' trainer set
  (primary + co-trainers), not `class.trainer_id` alone.

## Migration and rollout

- Backend image runs `python -m alembic upgrade head` on every container start
  (`docker-entrypoint.py`); a failed migration stops the container instead of
  booting a half-migrated schema.
- Local databases created by the old `create_all` startup (new tables, missing
  columns, `DuplicateTable` on upgrade) need a one-time
  `docker compose down -v && docker compose up -d --build`.
- Tests build their own schema with `Base.metadata.create_all`; they do not run
  Alembic.

## Test coverage

`tests/test_prerequisites.py` (3 tests):

1. Admin create with `prerequisite_class_ids`, list shows them, `PATCH` replaces
   them, self-reference rejected.
2. Catalog marks a class blocked for a student without a `PASSED` result; join
   request returns `400`; after the trainer marks `PASSED` the request succeeds
   and approval works.
3. Result endpoint accepts only the class trainer (`403` for TA and admin) and
   validates the result value.

Full suite: `uv run python -m pytest -q` → 276 passed; `uv run ruff check app tests`
clean.
