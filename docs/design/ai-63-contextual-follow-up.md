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
   HTTP errors rather than broken SSE streams. Turns refused by the class scope
   gate are excluded from this context, while still being returned by the
   history API so the conversation reloads unchanged.
2. The class scope gate and the follow-up rewrite are independent, so
   `stream_chat()` starts both as background tasks immediately and bounds them
   with one wall-clock deadline, `CHAT_PRE_RETRIEVAL_TIMEOUT_SECONDS`. The gate
   is awaited first: if it blocks, the rewrite is cancelled and the refusal is
   streamed without waiting for it. Otherwise the rewrite result is used once
   ready. Both calls go through the admin-managed key pools
   (`gemini_key_service`), so a rejected or rate-limited
   key rotates to the next healthy one instead of failing the turn. Provider or
   prompt failures, and a rewrite that misses the deadline, fall back to the
   original question qualified with the class name and description, so a failed
   rewrite never reaches retrieval or web search as a bare fragment. A gate that
   misses the deadline fails open and retrieval proceeds.
3. Retrieval and reranking use the standalone question, so pronouns and omitted
   subjects resolve before Qdrant search.
4. The answer prompt receives the same bounded history for reference resolution.
   Old assistant citation markers are stripped, and the system prompt forbids
   treating previous answers as evidence.
5. The completed turn is saved through `history_service.save_chat_turn()` with
   the citations attached to the assistant message. A refused turn is saved
   with `excluded_from_context=True`.
6. History reload returns each answer's original citations, so a later turn
   cannot overwrite an earlier answer's source list.

## Configuration

| Setting | Default | Meaning |
|---|---:|---|
| `CHAT_CONTEXT_EXCHANGES` | `5` | Number of complete previous Q&A pairs |
| `CHAT_CONTEXT_MAX_CHARS` | `6000` | Character budget for the history prompt |
| `CHAT_CLASS_CONTEXT_MAX_CHARS` | `240` | Description budget used to qualify a failed rewrite |
| `CHAT_PRE_RETRIEVAL_TIMEOUT_SECONDS` | `15` | Shared deadline for the class gate plus follow-up rewrite |
| `CHAT_CONDENSE_TIMEOUT_SECONDS` | `10` | Per-attempt cap for the rewrite inside the shared deadline |
| `CHAT_FALLBACK_MODEL` | `models/gemini-flash-lite-latest` | Rewrite model tried when the chat model fails |
| `LOG_LEVEL` | `INFO` | Application log level; `INFO` keeps the per-turn timing lines |

The gate's own budget, fallback model, cooldown and key rotation are covered in
[AI-61](ai-61-rag-qa-backend.md#class-scope-gate) (`DECISION_*`).

## Latency

The gate and rewrite used to run sequentially: `DECISION_TIMEOUT_SECONDS` (12s)
plus up to two `CHAT_CONDENSE_TIMEOUT_SECONDS` attempts (10s each), so a slow
provider could delay retrieval by more than 30s. Both now start at the same
time and share `CHAT_PRE_RETRIEVAL_TIMEOUT_SECONDS`, which bounds the whole
pre-retrieval phase. The primary and fallback rewrite attempts split the
remaining budget instead of each receiving a fresh timeout. A blocking gate is
no longer delayed by a rewrite that will be discarded.

`CHAT_TIMEOUT_SECONDS` still bounds the whole turn, including retrieval and
answer generation; the pre-retrieval deadline only covers the scope decision
and the standalone-question rewrite.

## Observability

With `LOG_LEVEL=INFO`, each turn logs the decision path and timings:

- `Chat turn started: class=… session=… history=… message_chars=…`
- `Scope gate blocked the turn after 1.23s (class=…, rewrite_cancelled=True)`
- `Follow-up rewrite completed in 0.84s (18 -> 57 chars)`
- `Follow-up rewrite exceeded its deadline after 15.00s; using the class-qualified question.`
- `Scope gate exceeded the 15.0s pre-retrieval deadline; failing open.`
- `Scope decision primary models/… failed after 0.89s (503 UNAVAILABLE).`
- `Scope decision out_of_scope (1.00) via gemini/models/gemini-flash-lite-latest fallback in 2.33s.`
- `Scope decision primary models/… skipped for another 28.1s (cooldown); using fallback models/…`
- `Pre-retrieval finished in 3.41s (gate=0.92s, rewrite=yes, question_chars=57, class=…)`

## Persistence

`chat_message.citations` is a JSON column (migration
`0018_chat_message_citations`) holding the same shape emitted by the SSE
`citation` event. It is nullable for rows created before AI-63 and is exposed by
`ChatMessageResponse`.

`chat_message.excluded_from_context` (migration `0024_chat_context_exclusion`)
marks both rows of a refused turn. `load_recent_exchanges()` filters on it, so
the turn is never replayed as context; history endpoints still return it.

## Tests

- `tests/services/chat/test_chat_context.py`: pair selection, ownership/class
  checks, character budget, citation-marker stripping, rewrite fallback, and the
  shared rewrite deadline.
- `tests/services/chat/test_chat_context_flow.py`: follow-up rewrite reaches
  retrieval, both turns are persisted, citations survive reload, and the gate
  and rewrite run in parallel under the shared deadline (block cancels the
  rewrite, a stuck rewrite falls back to the class-qualified question, a stuck
  gate fails open).
- `tests/chat/test_router.py`: `session_id` context loading and cross-user
  rejection.
- `tests/test_history.py`: citation round-trip through the history API.
- Frontend `lib/sse.test.ts`: `done` event carries conversation IDs.

## Known boundary

History is used only for reference resolution, never as evidence. The current
streaming path remains document-only (AI-61); AI-65 source routing and AI-67
Internet search are outside this flow.
