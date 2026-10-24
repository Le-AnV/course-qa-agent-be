# AI-95 - Rename a Chat Conversation

> **Ticket:** AI-95 (Epic E7, chat history)
> **Repo:** `course-qa-agent-be`
> **Target branch:** `features/Design_ChatRename`
> **Status:** Design approved, implemented on `features/Implementation_ChatRename`

## Purpose

A session is auto-titled from the first 50 characters of its first question
(AI-95 AC-03). That title is often a poor summary once the conversation moves on.
This design adds an owner-only rename operation that changes **only the title**:
the transcript is untouched and the sidebar keeps its recent-activity order.

## Endpoint

```
PATCH /api/history/{session_id}
```

| Item | Value |
|---|---|
| Auth | Bearer token, same dependency as the other history routes |
| Path param | `session_id` - UUID of the chat session |
| Body | `SessionRenameRequest` |
| Response | `200` + `ChatSessionResponse` |
| Errors | `401` unauthenticated, `403` not the owner, `404` unknown session, `422` blank/too long title |

### Request

```json
{
  "title": "  Week 3 - logistics review  "
}
```

### Response

```json
{
  "id": "0f0c5f9e-1f4b-4f0e-9b0a-9f6f0b0f3f11",
  "class_id": "a7a1f0c2-5d0e-4b8e-9f6b-2f7f0e7e2f10",
  "title": "Week 3 - logistics review",
  "created_at": "2026-10-20T09:12:44Z",
  "updated_at": "2026-10-24T08:02:19Z"
}
```

## Validation rules

| Rule | Behaviour |
|---|---|
| `title` is required | `422` from Pydantic |
| Length 1..255 before trimming | `422` |
| Whitespace-only title | `ValueError("Title must not be empty.")` -> `422` |
| Surrounding whitespace | Trimmed before it is stored and returned |

The schema stays in `app/schemas/history.py` as `SessionRenameRequest`. The
validator runs before the route, so the service always receives a clean value.

## Authorization and scoping

1. `get_current_user` guarantees an authenticated caller.
2. The service loads the session by id; a missing row is `404` (it does not reveal
   whether the session belongs to somebody else).
3. If `session.user_id != current_user.id` the service returns `403`.
4. `class_id` is never accepted from the client: a rename cannot move a session to
   another class.

## Ordering guarantee

`chat_session.updated_at` drives the sidebar order (newest activity first). A rename
must not be treated as activity, so the service issues an explicit
`UPDATE chat_session SET title = :title, updated_at = chat_session.updated_at`.
Assigning the column to itself bypasses the model's `onupdate` hook, so a rename
never reorders the list. This is verified by a regression test that compares
`updated_at` before and after the call.

## Flow

```
Client                     Router                          Service / DB
  |  PATCH /api/history/{id}  |                                  |
  |-------------------------->|                                  |
  |                           | get_current_user                 |
  |                           | rename_session(db, id, user, t)  |
  |                           |--------------------------------->| SELECT session
  |                           |                                  | 404 if missing
  |                           |                                  | 403 if not owner
  |                           |                                  | UPDATE title, updated_at = updated_at
  |                           |<---------------------------------| COMMIT + refresh
  |<--------------------------| 200 ChatSessionResponse          |
```

## Error responses

| Status | `detail` |
|---|---|
| 403 | `Access forbidden: you do not own this chat session.` |
| 404 | `Chat session not found.` |
| 422 | Pydantic message for a blank or oversized title |

## Test plan

| Test | Assertion |
|---|---|
| `test_rename_session_updates_title_without_bumping_activity` | Title is trimmed, persisted, and `updated_at` is unchanged |
| `test_rename_session_rejects_another_users_session` | `403` for a session owned by another user |
| `test_rename_session_rejects_blank_title` | `422` for a whitespace-only title |

## Related

- AI-95 - Save & Display Chat History (`docs/design/ai-95-chat-history.md`).
- AI-63 - Contextual Follow-up Questions - per-answer citations persist with the
  assistant message; renaming must not affect them.
- Frontend: `course-qa-agent-fe/docs/design/ai-95-chat-rename-ui.md`.
