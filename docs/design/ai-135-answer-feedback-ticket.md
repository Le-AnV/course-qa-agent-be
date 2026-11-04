# AI-135 Answer Feedback and Confirmed Ticket Creation

## Purpose

This document defines the backend contract for collecting feedback on completed AI answers and creating a teacher ticket only after the student confirms. The ticket preserves the answer context and citations so the assigned trainer can review the same information.

## Scope and requirements

- A student can set or change an answer's feedback to `helpful` or `not_helpful`.
- Feedback is available only for a completed, non-empty assistant message owned by the student.
- `not_helpful` is a prerequisite for feedback-originated ticket creation; `helpful` never creates a ticket.
- A ticket is created only through an explicit confirmation request.
- The server derives the class, original question, answer, and citations from persisted chat data. The client can submit optional details only.
- At most one feedback-originated ticket can exist per answer. Retries return the existing ticket.
- Existing manual ticket creation remains supported.

## Data model

### ChatMessage

- `feedback`: nullable string constrained to `helpful` or `not_helpful`. Existing messages remain `NULL` until the student selects a value.
- `parent_message_id`: nullable self-reference to the original USER message, with `ON DELETE SET NULL`.
- New chat turns save the USER message first and link the ASSISTANT answer to it in the same transaction.
- For legacy history, migration links only an unambiguous adjacent USER/ASSISTANT pair in the same session. Equal or ambiguous timestamps remain unlinked.

### Ticket

- `citations`: a non-null JSON snapshot with an empty-list default for older/manual tickets.
- `source_message_id`: nullable, unique foreign key to the originating assistant message with `ON DELETE SET NULL`.
- Question, complete assistant answer, class ID, citations, and source message are snapshotted when the ticket is created. Deleting chat later does not delete the ticket or its snapshots.

## API contract

### Save or change answer feedback

`PUT /api/chat/messages/{message_id}/feedback`

Request:

```json
{ "feedback": "not_helpful" }
```

`feedback` must be `helpful` or `not_helpful`. The response returns the stored current selection:

```json
{ "feedback": "not_helpful" }
```

### Create a ticket after confirmation

`POST /api/chat/messages/{message_id}/ticket`

Request:

```json
{ "confirmed": true, "details": "Please explain the second step." }
```

`confirmed` must be the boolean `true`. `details` is optional and limited to 10,000 characters. The server ignores no client-provided snapshots because the request schema rejects extra fields. A newly created ticket returns `201`; an idempotent retry returns the existing ticket with `200`.

The response uses the standard ticket response and includes `citations` and nullable `source_message_id`:

```json
{
  "id": "<ticket-uuid>",
  "class_id": "<class-uuid>",
  "student_id": "<student-uuid>",
  "question": "<original question>",
  "original_question": "<original question>",
  "agent_answer": "<complete assistant answer>",
  "citations": [],
  "source_message_id": "<assistant-message-uuid>",
  "status": "OPEN",
  "created_at": "<timestamp>"
}
```

## History and ticket responses

Chat history messages add:

- `feedback`: nullable current selection.
- `ticket_id`: nullable ID of the feedback-originated ticket.
- `original_question`: nullable prompt resolved through the stored parent link.
- `can_create_feedback_ticket`: whether a valid original USER message is linked in the same session.

Ticket responses, including trainer inbox items and ticket threads, add `citations` and `source_message_id`. Manual ticket callers continue to use `POST /api/tickets` and may omit these fields.

## Authorization, validation, and errors

- Non-students receive `403` on feedback and feedback-ticket endpoints.
- Missing messages and messages owned by another user return `404`.
- A USER/SYSTEM message or an empty/incomplete answer is rejected with `409`.
- Ticket creation requires current `not_helpful` feedback and an identifiable linked USER prompt; otherwise it returns `409`.
- The existing ticket service enforces student role, active class, and active enrollment. A class or enrollment that is no longer active prevents a new ticket.
- A previously created ticket is returned on retry before rechecking current feedback or enrollment, preserving idempotency.
- A database unique constraint on `source_message_id` protects concurrent requests. On a uniqueness conflict, the service returns the winning ticket.

## Transaction and migration behavior

Ticket creation reuses `create_student_ticket`, which stores the ticket, snapshots, and optional initial details reply in one transaction. The unique source constraint makes concurrent retries safe. Migration defaults preserve old data: feedback and source IDs are `NULL`, citations are `[]`.

Apply the schema migration before deploying the backend, then deploy the frontend.

## Acceptance criteria mapping

| AC | Backend behavior |
|---|---|
| AC-01 | Feedback endpoints accept only completed assistant answers. |
| AC-02 | Negative feedback is persisted; ticket requires an explicit confirmed request. |
| AC-03 | The server snapshots answer context and citations into a class ticket visible through trainer ticket APIs. |
| AC-04 | Feedback persists in history; helpful feedback does not create a ticket. |
| AC-05 | Feedback updates and history reads never create tickets; ticket creation occurs only on the confirmed endpoint. |

## Verification scenarios

- Change feedback in both directions and verify history returns the latest value.
- Verify role, ownership, inactive class/enrollment, and missing original prompt rejection.
- Retry and concurrently submit a confirmed request; verify one ticket and one initial details reply.
- Delete the chat session after creation; verify the ticket and snapshots remain and `source_message_id` becomes `NULL`.
- Verify empty citations and manually created tickets remain compatible.
