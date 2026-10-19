# AI-129 — Class Information Page

> **Ticket:** AI-129 (parent AI-23) · **Status:** Implemented (backend)
> **Branch:** `features/Design_ClassManagement` · **Companion UI design:**
> `course-qa-agent-fe/docs/design/ai-129-class-information-page.md`

## Purpose

Each class has one information page — description, meeting link, group link,
schedule note and a free note — so the practical details of a class live with the
class instead of being scattered across chat, email and shared docs. Trainers and
admins write it; enrolled students read it.

## Scope and confirmed team decisions

- One information row per class (1:1), table `class_info`
  (migration `0010_class_info`); every field is optional.
- Write access: the primary trainer, any co-trainer, or an admin — the same
  `_require_class_teacher` gate as the configuration surface.
- Read access: the class' staff and admins always; enrolled active students of a
  **live** class. An archived class page is readable only by its staff and admins.
- Links must be `http`/`https` URLs; anything else is rejected at the schema layer
  (`422`).
- Partial updates: omitted fields keep their value, an empty string clears a
  field (the writer sends only what changed).
- Field limits: description 2000, note 2000, links 500, schedule note 200.

## API design

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /api/classes/{class_id}/info` | Admin / class staff / enrolled student | Read the class page. |
| `PATCH /api/classes/{class_id}/info` | Admin / class staff | Partial update; `""` clears a field. |

`ClassInfoRead` contract:

```json
{
  "description": "Weekly seminar on conversational QA…",
  "meeting_link": "https://meet.google.com/abc-defg-hij",
  "group_link": "https://zalo.me/g/xyz",
  "schedule_note": "Mon & Wed 19:00–21:00, online",
  "note": "Group registration in week 1."
}
```

Read rules in order:

- Unknown class → `404`.
- Admin → always allowed.
- A trainer of the class (primary or co) → allowed, including archived classes.
- Anyone else → allowed only for a live class they are an active member of;
  otherwise `403` (archived class, not enrolled, or pending membership).

## Backend responsibilities

- `app/models/course.py` — `ClassInfo` model (1:1 with `Class`).
- `alembic/versions/0010_class_info.py` — the information table.
- `app/schemas/admin.py` — `ClassInfoRead`, `ClassInfoUpdate` (length limits and
  URL validation; blank clears).
- `app/services/course_service.py` — `get_info` / `update_info` with partial
  semantics; `is_active_member` backs the student read rule.
- `app/routers/class_config.py` — `GET`/`PATCH /api/classes/{id}/info`.

## Edge cases

- A trainer who does not teach the class receives `403` on write (and on read of
  an archived class page).
- Pending (not yet approved) students cannot read the page: `is_active_member`
  requires an ACTIVE membership.
- Clearing every field leaves an empty page — the read side returns `null`s and
  the UI hides empty rows.

## Test coverage

- `tests/test_class_info.py` (6 tests): enrolled students read but never write
  (`403`), outsider students are blocked (`403`), another trainer cannot write
  (`403`), invalid links rejected (`422`), over-length fields rejected (`422`),
  partial update and clear semantics.

Run from the backend repository root:

```powershell
uv run python -m pytest tests/test_class_info.py -q
uv run ruff check .
```

## Acceptance-criteria mapping

| Jira criterion | Agreed implementation |
|---|---|
| AC-01: Trainers (primary or co) and admins write; enrolled students read | `_require_class_teacher` on `PATCH`; read rule walks admin → class trainer → active membership. |
| AC-02: Links must be http/https URLs | `ClassInfoUpdate` validates both links; anything else is `422`. |
| AC-03: Field limits (2000/500/200) | Schema `max_length` bounds; over-length input is `422`. |
| AC-04: Partial update; `""` clears | `exclude_unset` semantics in `update_info`. |
| AC-05: Non-teaching trainer cannot write (`403`); archived page readable only by staff and admins | Role checks in `read_info`/`_require_class_teacher`; archived classes skip the student read path. |

## Branch and merge-request convention

Keep the design and implementation deliverables separate:

- Design branch: `features/Design_ClassManagement`; contains this document and its
  siblings for AI-127, AI-128 and AI-130 under `docs/design/`.
- Implementation branch: `features/Implementation_ClassManagement`; contains the
  backend code, migrations and tests, but not these design documents.

Create separate merge requests for the two branches and link both to AI-127..131.
Do not mark review, CI, or manual-test checklist items complete without evidence.

## Out of scope

- Rich text or attachments on the class page — fields stay plain text/URLs.
- Per-session schedules; the schedule note is a free-text summary.
- Notifications when the page changes.
