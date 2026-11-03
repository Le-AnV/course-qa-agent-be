# AI-81 — Student question ticket lifecycle

## Purpose and scope

An authenticated student can explicitly send a question to the teacher assigned
to an active class in which the student is enrolled. The ticket keeps the
student's submitted question, the original chat question, and the assistant's
answer when provided. Chat messages do not create tickets automatically.

The ticket lifecycle includes student follow-ups, a trainer reply, student
confirmation, and automatic expiration of unanswered tickets. The trainer
inbox presentation is handled by the companion AI-83 frontend task.

## Data model

- `ticket`: UUID `id`, `class_id`, `student_id`, `question`, nullable
  `original_question`, nullable `agent_answer`, `status`, and `created_at`.
- `ticket_reply`: UUID `id`, `ticket_id`, `user_id`, `message`, and
  `created_at`. Student details and follow-ups and trainer replies are stored
  here.
- Deleting a ticket cascades its replies. Class and student references are
  protected from deletion.
- Status values are `OPEN`, `ANSWERED`, `RESOLVED`, and `EXPIRED`.

## Lifecycle

| Status | Transition |
|---|---|
| `OPEN` | A student creates the ticket. Student follow-ups leave it open. An unanswered ticket becomes `EXPIRED` three days after creation. |
| `ANSWERED` | The assigned trainer replies to an open ticket. |
| `RESOLVED` | The student confirms the trainer's answer resolved the question. Only answered tickets can be resolved. |
| `EXPIRED` | The system expires an open ticket after three days without a trainer reply. Expired tickets cannot receive replies or be resolved. |

Expiration runs in a background task every 60 seconds and is also checked when
ticket lists, ticket details, or replies are accessed.

## API contract

All routes require authentication. UUIDs are serialized as strings and
timestamps as ISO-8601.

### `POST /api/tickets`

Students create a ticket for an active class in which they have an active
enrollment. `student_id` is derived from the authenticated user.

Request:

```json
{
  "class_id": "<class UUID>",
  "question": "<question sent to the teacher>",
  "original_question": "<optional original chat question>",
  "agent_answer": "<optional assistant answer>",
  "details": "<optional student context>"
}
```

`class_id` and `question` are required. Text fields are trimmed and limited to
10,000 characters; `question` must not be empty. Empty optional text is treated
as absent. Optional `details` is stored as the first ticket reply.

Returns `201 Created` with the ticket ID, class and student IDs, question,
optional original question and assistant answer, `OPEN` status, and creation
time. A caller without the student role, an inactive or missing class, or a
student without active enrollment receives `403`. Invalid request data receives
a validation error.

### `GET /api/tickets/{ticket_id}`

The student who owns the ticket reads its details and replies. A different
student or unknown ticket ID receives `404`; users with another role receive
`403`.

### `POST /api/tickets/{ticket_id}/replies`

Students may add a follow-up to their own `OPEN` ticket. A trainer assigned to
the ticket's class may reply to an `OPEN` ticket; the trainer reply changes its
status to `ANSWERED`.

Request: `{ "message": "<reply>" }`. The message is trimmed, must not be empty,
and is limited to 10,000 characters. Returns `201 Created` with the reply ID,
ticket ID, author ID, message, and timestamp. A non-assigned trainer receives
`403`; an unauthorized student ticket lookup returns `404`; a ticket that
cannot receive a reply returns `409`.

### `POST /api/tickets/{ticket_id}/resolve`

The owning student confirms an `ANSWERED` ticket and changes its status to
`RESOLVED`. A ticket not found for that student returns `404`; a ticket that is
not `ANSWERED` returns `409`.

### `GET /api/tickets`

Trainers list tickets only for their assigned classes. Optional query
parameters are `class_id`, `status`, `page` (default `1`), and `page_size`
(default `20`, maximum `100`). Results are ordered newest first and returned
with `items`, `total`, `page`, and `page_size`. Filtering by a class not
assigned to the trainer returns `403`.

## Database migrations

- `0028_student_tickets` creates `ticket` and `ticket_reply`.
- `0029_ticket_agent_answer` adds the optional assistant answer.
- `0030_ticket_original_question` adds the optional original chat question.
- `0031_ticket_lifecycle` allows the `EXPIRED` status and normalizes existing
  expired statuses to `OPEN` when downgraded.

## Verification

Focused backend tests:

```powershell
uv run pytest tests/tickets/test_ticket_router.py tests/tickets/test_migration.py -q
```
