# AI-99 — Delete a Chat Session

> **Ticket:** [AI-99](https://ai03.atlassian.net/browse/AI-99) (Epic AI-93: Chat History & Memory) · **Priority:** Medium
>
> **Assignee:** Bui Trong Suu
>
> **Status:** Design
>
> **Related stories:** AI-95 (Chat History & Sidebar UI), AI-97 (Reopen an Old Conversation), AI-63 (Contextual Follow-up Questions).

---

## 1. Purpose

Allow an authenticated student to delete one of their own chat sessions. The operation removes the session and its messages from history while preventing cross-user deletion.

Acceptance criteria:

* **AC-01:** The UI asks for confirmation before deleting; deleting one session must not affect any other session.
* **AC-02:** Only the session owner can delete it. A different authenticated user receives `403 Forbidden`.
* **AC-03:** After deletion, the session no longer appears in the sidebar/history list and cannot be opened again (`GET /api/history/{session_id}` returns `404`).

---

## 2. API Contract

### 2.1 Request

* **Method:** `DELETE`
* **URL:** `/api/history/{session_id}`
* **Auth:** `Authorization: Bearer <JWT_ACCESS_TOKEN>`
* **Request body:** none

| Parameter | Location | Type | Required | Description |
|---|---|---|---|---|
| `session_id` | path | UUID | yes | Chat session to delete. |

### 2.2 Success Response

`200 OK` — `DeleteSessionResponse`

```json
{
  "message": "Chat session deleted successfully."
}
```

### 2.3 Error Responses

| Status | Condition | Body |
|---|---|---|
| `401 Unauthorized` | Missing, invalid, or expired token | `{"detail": "Not authenticated."}` or `{"detail": "Invalid token."}` |
| `403 Forbidden` | Session exists but belongs to another user | `{"detail": "Access forbidden: you do not own this chat session."}` |
| `404 Not Found` | `session_id` does not exist | `{"detail": "Chat session not found."}` |

The service checks existence before ownership: a missing session returns `404`, while an existing session owned by another user returns `403`. Neither response exposes session content.

---

## 3. Deletion and Cleanup

### 3.1 Ownership

`history_service.delete_session()` loads the session by `session_id` and compares `session.user_id` with the authenticated `current_user.id`. This comparison is the row-level security boundary; class membership does not grant permission to delete another user's session.

### 3.2 PostgreSQL Cascade

`ChatSession.messages` uses `cascade="all, delete-orphan"`, and `ChatMessage.session_id` has `ON DELETE CASCADE`. Deleting the `chat_session` row therefore removes all `chat_message` rows belonging to that session.

The session delete and message cascade are committed in one database transaction (`db.delete(session)` followed by `db.commit()`).

### 3.3 Qdrant Cleanup (Best-Effort)

Before deleting the database row, the service collects IDs of messages in the session where `used_web_search = true` and calls `delete_content_gap_queries()` to remove matching points from the `content_gap_queries` collection.

This cleanup is best-effort:

* A Qdrant connection or deletion failure is logged with `logger.warning(...)`.
* The failure must not block deletion of the PostgreSQL session.
* Qdrant and PostgreSQL are not part of one transaction.

Known limitation: current chat persistence calls `save_chat_turn(..., used_web_search=False)` and does not pass `query_embedding`, so production messages may not create points in `content_gap_queries`. The delete test suite exercises the cleanup path with a seeded `used_web_search = true` message. Fixing the upstream persistence gap is outside AI-99.

### 3.4 Operation Order

1. Authenticate the caller through `get_current_user`.
2. Load `chat_session` by `session_id`; return `404` if missing.
3. Compare `session.user_id` with `current_user.id`; return `403` if different.
4. Collect message IDs where `used_web_search = true`.
5. Delete matching Qdrant points (best-effort; log and continue on failure).
6. Call `db.delete(session)` and `db.commit()`; the database cascade removes `chat_message` rows.
7. Return `200 OK` with `DeleteSessionResponse`.

---

## 4. Automated Test Plan

| Test case | AC | Main assertion |
|---|---|---|
| `test_delete_session_does_not_affect_other_sessions` | AC-01 | Deleted session returns `404`; the other session remains in the list and still returns its messages. |
| `test_delete_session_rejects_another_user` | AC-02 | Non-owner receives `403` with the ownership error; the session remains in the database. |
| `test_delete_nonexistent_session_returns_404` | AC-03 | A random UUID returns `404 Not Found`. |
| `test_delete_session_purges_content_gap_vectors` | AC-03 | Qdrant cleanup is called with the ID of the `used_web_search = true` message; both messages are cascade-deleted. |

Run the delete tests:

```bash
uv run pytest -q tests/test_history.py -k "delete" -v
```

Run the full history suite to check for regressions:

```bash
uv run pytest -q tests/test_history.py -v
```

Expected: all `test_delete_*` tests pass with no errors.

---

## 5. Reference

* [AI-95 — Chat History & Sidebar UI](./ai-95-chat-history.md), §3.5 `DELETE /api/history/{session_id}`.
