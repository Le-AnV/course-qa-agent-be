# AI-63 — Contextual Follow-up Questions

> **Ticket:** AI-63 (Epic E4, parent AI-23) · **Status:** Implemented
> **Related stories:** AI-61 (RAG Q&A backend), AI-95 (chat history/citations).

## Purpose

Students can ask follow-ups such as "so when is the deadline?" without repeating
the topic. The chat keeps the last N complete Q&A exchanges, rewrites the
follow-up into a standalone retrieval question, and still answers only from the
current class evidence with fresh citations.

## Request contract

`POST /api/chat` now accepts an optional `session_id`:

```json
{
  "message": "So when is the deadline?",
  "class_id": "…",
  "session_id": "…"
}
```

`session_id` is `null` for a new chat. The backend creates the session on the
first persisted turn and returns it in the final `done` SSE event together with
`user_message_id` and `assistant_message_id`.

## Flow

1. The router verifies class access, then loads the last
   `CHAT_CONTEXT_EXCHANGES` complete pairs for the session. Ownership and class
   scope are checked before the stream starts, so 403/404 responses are normal
   HTTP errors rather than broken SSE streams.
2. `chat_context_service.condense_question()` rewrites the follow-up using the
   bounded conversation history. Provider or prompt failures fall back to the
   original question.
3. Retrieval and reranking use the standalone question, so pronouns and omitted
   subjects resolve before Qdrant search.
4. The answer prompt receives the same bounded history for reference resolution.
   Old assistant citation markers are stripped, and the system prompt forbids
   treating previous answers as evidence.
5. The completed turn is saved through `history_service.save_chat_turn()` with
   the citations attached to the assistant message.
6. History reload returns each answer's original citations, so a later turn
   cannot overwrite an earlier answer's source list.

## Configuration

| Setting | Default | Meaning |
|---|---:|---|
| `CHAT_CONTEXT_EXCHANGES` | `5` | Number of complete previous Q&A pairs |
| `CHAT_CONTEXT_MAX_CHARS` | `6000` | Character budget for the history prompt |

## Persistence

`chat_message.citations` is a JSON column (migration
`0018_chat_message_citations`) holding the same shape emitted by the SSE
`citation` event. It is nullable for rows created before AI-63 and is exposed by
`ChatMessageResponse`.

## Tests

- `tests/services/chat/test_chat_context.py`: pair selection, ownership/class
  checks, character budget, citation-marker stripping, rewrite fallback.
- `tests/services/chat/test_chat_context_flow.py`: follow-up rewrite reaches
  retrieval, both turns are persisted, citations survive reload.
- `tests/chat/test_router.py`: `session_id` context loading and cross-user
  rejection.
- `tests/test_history.py`: citation round-trip through the history API.
- Frontend `lib/sse.test.ts`: `done` event carries conversation IDs.

## Known boundary

History is used only for reference resolution, never as evidence. The current
streaming path remains document-only (AI-61); AI-65 source routing and AI-67
Internet search are outside this flow.
