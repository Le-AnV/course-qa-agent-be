# AI-89 — Ticket lifecycle, resolve/reopen, and per-class counts (API design)

**Status:** Draft for review
**Related:** AI-81 (student ticket API), AI-83 (trainer inbox UI), AI-89 (this task)
**Supersedes:** AI-81 expiry window (3 days → 7 days); extends AI-81 resolve flow with trainer actions.

---

## 1. Scope

AI-89 completes the student/trainer ticket lifecycle:

- A trainer assigned to the ticket's class can mark an `OPEN` or `ANSWERED` ticket as `RESOLVED`.
- A `RESOLVED` ticket can be reopened by the assigned trainer or by the student who owns it.
- Trainers can see open/completed ticket counts for every active class they teach.
- Archived classes are invisible to all ticket list/detail/count/reply/resolve/reopen operations.
- The expiry window becomes seven days and restarts when a resolved ticket is reopened or when a student follows up on an `ANSWERED` ticket.

Student behavior stays as defined by AI-81: a student may confirm `RESOLVED` only for their own `ANSWERED` ticket.

---

## 2. Actors and permissions

| Operation | Student (owner) | Trainer (assigned, active class) | Admin |
|---|---|---|---|
| Create ticket | Yes, active class + active enrollment | No | No |
| List own tickets (`/mine`) | Yes | No | No |
| List trainer inbox (`/`) | No | Yes | No |
| Read ticket detail (`/{id}`) | Yes, own + active class | Via inbox list only (`GET /{id}` is student-only) | No |
| Reply | Own `OPEN` / `ANSWERED` | Assigned `OPEN` only | No |
| Resolve | Own `ANSWERED` only | Assigned `OPEN` / `ANSWERED` | No |
| Reopen | Own `RESOLVED` | Assigned `RESOLVED` | No |
| Counts (`/counts`) | No | Yes, assigned active classes | No |
| Archived class | `404` | `403` | `403` |

Authorization is enforced server-side. The frontend is not an authorization boundary.

---

## 3. Status model

| Status | Meaning | Entered by |
|---|---|---|
| `OPEN` | Waiting for a trainer answer. | Student creates a ticket; student follows up on `ANSWERED`; reopen when no trainer reply exists. |
| `ANSWERED` | Trainer replied. | Trainer replies to `OPEN`; reopen when a trainer reply exists. |
| `RESOLVED` | Handled / confirmed. | Trainer resolves `OPEN`/`ANSWERED`; student confirms an `ANSWERED` ticket. |
| `EXPIRED` | No trainer answer within the expiry window. | System expiry sweep. |

### Allowed transitions

```text
create ──────────────► OPEN
OPEN + trainer reply ► ANSWERED
ANSWERED + student follow-up ► OPEN   (reopened_at = now)
OPEN/ANSWERED + trainer resolve ► RESOLVED
ANSWERED + student confirm ► RESOLVED
RESOLVED + reopen ► ANSWERED (trainer reply exists) | OPEN (no trainer reply)
OPEN + 7 days without answer ► EXPIRED
```

Invalid transitions return `409 Conflict`.

---

## 4. Expiry window

- Constant: `TICKET_EXPIRATION = 7 days`.
- A ticket expires only when it is `OPEN` and:

```text
coalesce(reopened_at, created_at) <= now - 7 days
```

- `reopened_at` is updated when:
  - a student follows up on an `ANSWERED` ticket (ticket returns to `OPEN`), or
  - a `RESOLVED` ticket is reopened.
- A follow-up on an already-`OPEN` ticket does **not** move the expiry anchor.
- Expiry is applied lazily by ticket service reads/writes, not by a background job.

---

## 5. Data model and migration

### `ticket`

Existing fields: `id`, `class_id`, `student_id`, `question`, `original_question`, `agent_answer`, `status`, `created_at`.

New field:

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `reopened_at` | `timestamptz` | Yes | Latest reopen/follow-up that restarted the expiry window. |

### Migration

- Revision: `0033_ticket_reopened_at`
- Depends on: `0032_quiz_schema` (renumbered from `0032_ticket_reopened_at` to avoid two Alembic heads after AI-148 was merged).
- Upgrade: add nullable `reopened_at`.
- Downgrade: drop `reopened_at`.
- Additive only; existing tickets keep `NULL` and continue using `created_at`.

---

## 6. API contract

Base path: `/api/tickets`

### 6.1 `GET /api/tickets` — trainer inbox

Query:

| Param | Type | Notes |
|---|---|---|
| `class_id` | UUID, optional | Must be an active class assigned to the trainer; otherwise `403`. |
| `status` | `OPEN \| ANSWERED \| RESOLVED \| EXPIRED`, optional | Omit for all statuses. |
| `page` | int ≥ 1, default 1 | |
| `page_size` | int 1..100, default 20 | |

Response `TicketListResponse`:

```json
{
  "items": [
    {
      "id": "uuid",
      "class_id": "uuid",
      "student_id": "uuid",
      "question": "Why does this pointer fail?",
      "original_question": "Explain pointers",
      "agent_answer": "A pointer holds...",
      "status": "OPEN",
      "created_at": "2026-11-02T10:00:00Z",
      "student_name": "Academic Student",
      "class_name": "C++",
      "class_code": "C++",
      "class_status": "ACTIVE",
      "replies": []
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

Ordering: `created_at DESC, id DESC`.

### 6.2 `GET /api/tickets/counts` — per-class counts

Query: `class_id` optional. Must be assigned and active; otherwise `403`.

Response:

```json
{
  "items": [
    {
      "class_id": "uuid",
      "class_code": "C++",
      "class_name": "C++",
      "class_status": "ACTIVE",
      "open_count": 4,
      "completed_count": 2,
      "expired_count": 1
    }
  ],
  "open_total": 4,
  "completed_total": 2
}
```

Semantics:

- `open_count = OPEN + ANSWERED`
- `completed_count = RESOLVED`
- `expired_count = EXPIRED` (reported separately)
- Archived classes and their tickets are excluded from both items and totals.
- Classes are ordered by `code`.

### 6.3 `GET /api/tickets/mine` — student list

Student only. Same query shape as the trainer list (`class_id`, `status`, `page`, `page_size`). Only the caller's tickets are returned; archived classes are excluded.

### 6.4 `GET /api/tickets/{id}` — student detail

Student only. Returns `TicketThreadRead` with `replies`.

- Not owner, archived class, or unknown ID → `404`.
- Trainer/admin → `403`.
- Trainers read the thread from the inbox list response (`replies` is included there).

### 6.5 `POST /api/tickets` — create

Student only; class must be `ACTIVE` and the student must have an `ACTIVE` enrollment.

Request:

```json
{
  "class_id": "uuid",
  "question": "What does this error mean?",
  "original_question": "Explain this C++ error",
  "agent_answer": "The compiler reports...",
  "details": "Optional first message"
}
```

Response `201` with `TicketRead`, status `OPEN`.

### 6.6 `POST /api/tickets/{id}/replies` — reply / follow-up

Request: `{ "message": "..." }` (1..10000 chars).

| Role | Allowed ticket status | Effect |
|---|---|---|
| Student | `OPEN` or `ANSWERED` | Adds reply. If `ANSWERED`, status becomes `OPEN` and `reopened_at = now`. |
| Trainer | `OPEN` | Adds reply and status becomes `ANSWERED`. |

Errors: `403` wrong role/class, `404` not visible, `409` invalid status.

### 6.7 `POST /api/tickets/{id}/resolve`

| Role | Allowed status | Result |
|---|---|---|
| Student owner | `ANSWERED` | `RESOLVED` |
| Assigned trainer | `OPEN` or `ANSWERED` | `RESOLVED` |
| Admin/other | — | `403` |

Errors: `404` not visible, `409` invalid status.

### 6.8 `POST /api/tickets/{id}/reopen`

| Role | Allowed |
|---|---|
| Student owner | Own `RESOLVED` ticket |
| Assigned trainer | Assigned `RESOLVED` ticket |
| Admin/other | `403` |

Effect:

- `reopened_at = now`
- Status becomes `ANSWERED` if a non-student reply exists, otherwise `OPEN`.
- `409` when the ticket is not `RESOLVED`.

---

## 7. Archived classes

An archived class is treated as if its tickets do not exist:

- Trainer list/counts: class is removed from `class_ids_for_trainer(active_only=True)`; explicit `class_id` → `403`.
- Trainer detail/reply/resolve/reopen: `get_trainer_ticket` rejects the class → `403`.
- Student list: join `Class` and filter `Class.status != 'ARCHIVED'`.
- Student detail/reply/resolve/reopen: `get_student_ticket` filters archived classes → `404`.
- Create: only `Class.status == 'ACTIVE'`.

No data is deleted when a class is archived.

---

## 8. Error model

| Code | Case |
|---|---|
| `400` | Business validation that is not a schema error. |
| `403` | Wrong role, trainer not assigned, archived class for trainer. |
| `404` | Student ticket not found/not owned/archived class. |
| `409` | Invalid status transition (resolve/reopen/reply). |
| `422` | Schema validation (empty question/message, length limits, bad UUID). |

---

## 9. AC mapping

| AC | Implementation |
|---|---|
| AC-01: trainer marks handled → `RESOLVED` | `POST /{id}/resolve`, assigned trainer, `OPEN`/`ANSWERED`. |
| AC-02: `RESOLVED` can reopen | `POST /{id}/reopen`; returns `OPEN`/`ANSWERED`, sets `reopened_at`. |
| AC-03: counts per class | `GET /counts`; `open = OPEN+ANSWERED`, `completed = RESOLVED`. |
| Seven-day expiry | `TICKET_EXPIRATION = 7 days`; `coalesce(reopened_at, created_at)`. |
| Archived classes hidden | Active-only class scope in every ticket service method. |
| Student follow-up | `POST /{id}/replies`; `ANSWERED → OPEN` + `reopened_at`. |

---

## 10. Verification plan

- Router tests for the role matrix (student/trainer/admin).
- Lifecycle tests: create → reply → resolve → reopen → follow-up.
- Expiry tests: 7-day boundary; `reopened_at` restarts the window.
- Counts tests: `OPEN+ANSWERED`, `RESOLVED`, `EXPIRED`, archived exclusion.
- Migration test: upgrade adds nullable `reopened_at`; Alembic has a single head.
- Archived class tests: trainer `403`, student `404`, absent from lists/counts.

---

## 11. Open decisions

1. Student `Confirm resolved` is intentionally retained (AI-81 behaviour), limited to `ANSWERED`.
2. Expiry runs lazily on read/write; no scheduler is added in this task.
3. Trainer detail is read from the inbox response; a dedicated trainer `GET /{id}` endpoint is out of scope.
