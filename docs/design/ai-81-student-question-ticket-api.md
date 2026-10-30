# AI-81 — Student question ticket API design

## Purpose and scope

Allow an authenticated student to explicitly send a question to a trainer for a class in which the student is actively enrolled. The ticket preserves the student's question and optional excerpt from the assistant answer. The student may add details as an initial reply or add a follow-up reply later. The system never creates a ticket automatically.

Ticket reply notifications, a student ticket history/list page, trainer answers, resolution and reopening are separate work and are outside this contract.

## Data model

- `Ticket`: UUID `id`, `class_id`, `student_id`, `question`, nullable `agent_answer`, `status`, `created_at`.
- New tickets start as `OPEN`. Allowed lifecycle values are `OPEN`, `ANSWERED`, and `RESOLVED`; lifecycle transitions are outside AI-81.
- `TicketReply`: UUID `id`, `ticket_id`, `user_id`, `message`, `created_at`. Replies preserve initial details and later student follow-ups.
- Deleting a ticket cascades its replies. Class and student references are protected from deletion.

## API contract

All routes require the existing authenticated-user dependency. JSON is used for request and response bodies. UUIDs are serialized as strings; timestamps are ISO-8601.

### `POST /api/tickets`

Student explicitly creates a ticket.

Request:

```json
{
  "class_id": "<class UUID>",
  "question": "<student-authored question>",
  "agent_answer": "<optional answer excerpt>",
  "details": "<optional initial detail>"
}
```

`question` is required, trimmed, and 1–10,000 characters. `agent_answer` and `details` are optional, trimmed, and at most 10,000 characters. An empty optional string is treated as absent.

Return `201 Created` with the ticket ID, class, student, question, optional answer excerpt, `OPEN` status, and creation time. A caller without the student role receives `403`. A missing/inactive class or a student without active enrollment receives `403`. Invalid payloads receive FastAPI validation errors.

### `POST /api/tickets/{ticket_id}/replies`

The owning student adds a detail/follow-up message. Request: `{ "message": "..." }`; message is trimmed and 1–10,000 characters. Return `201 Created` with reply ID, ticket ID, author ID, message, and timestamp. A non-student receives `403`; a ticket not owned by the student returns `404` to avoid disclosing another student's ticket.

## Authorization and transaction rules

- Derive student identity from the authenticated user; never accept `student_id` from the request.
- Verify active class and active enrollment on creation.
- Restrict reply reads/writes to the ticket owner. Reply creation does not change ticket status.
- Persist ticket and optional initial reply in one database transaction.
- The student must explicitly submit the form; rendering an answer or giving negative feedback alone has no server-side effect.

## Compatibility and verification

The nullable `agent_answer` field preserves compatibility with clients that only send a question. Migration 0029 adds it after the ticket table migration; existing tickets remain valid.

Tests should cover successful creation and follow-up, role and enrollment authorization, inactive/missing class, ownership isolation, input validation, and persistence of optional fields. The implementation's focused command is `uv run pytest tests/tickets -q`.
