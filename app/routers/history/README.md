# routers/history — Epic E7: Chat History (T30–T33)

| Jira | Story | Assignee | Status |
|---|---|---|---|
| AI-95 | Save & Display Chat History | Bui Trong Suu | In Progress |
| AI-97 | Reopen an Old Conversation & Continue | Unassigned | To Do |
| AI-99 | Delete Chat History | Unassigned | To Do |
| AI-101 | Search Chat History | Unassigned | To Do |

**Key notes:**
- Tables: `conversations`, `messages` in PostgreSQL (see `models/chat.py`).
- Context window: follow-ups use prior messages (pairs with AI-63).
- When `used_web_search = true`, the **query embedding is also written to Qdrant `content_gap_queries`** (0 extra API calls — reuse of the T18 embed, TECHSTACK §3.3).
- Deleting a history also deletes its points from `content_gap_queries` (by `message_id`).

Prefix: `/api/history`.
