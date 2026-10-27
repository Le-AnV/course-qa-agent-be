# routers/chat — Epic E4: Contextual Q&A (T15–T20) — 48đ, lớn nhất

SSE streaming chat endpoint + agent orchestration. Prefix: `/api/chat`.

| Jira | Story | Assignee | Status |
|---|---|---|---|
| AI-59 | Student Chat Interface (FE part) | Nguyễn Chí Trung | To Do |
| AI-61 | RAG Q&A Backend (retrieve + generate) | Unassigned | Done |
| AI-63 | Contextual Follow-up Questions | Unassigned | Done |
| AI-65 | Retrieval Score Threshold (KB vs Internet) | Unassigned | To Do |
| AI-67 | Internet Source Agent | Unassigned | To Do |
| AI-69 | Chat Processing Status & System Errors | Unassigned | To Do |

**AI-61 document-only chat flow:**
1. Embed the query and retrieve up to 20 chunks from Qdrant, filtered by the requested class; the API verifies the caller's class access.
2. Keep only indexed documents and rerank candidates with the configured multilingual Cross Encoder.
3. Send the best five chunks to one grounded Gemini answer stream. The answer prompt requires an abstention when the selected passages do not support the answer.
4. Stream retrieval/generation progress stages, answer tokens, and citations via SSE; preserve the query language. If a Gemini key fails after text has started, the stream emits `restart` so the client clears the partial answer before the next healthy key continues.

The streaming AI-61 endpoint does not search the web. AI-65's separate source-routing service and AI-67's Internet provider remain outside this document-only chat path. The Cross Encoder model is downloaded on first use and cached in the Compose `reranker_cache` volume.

**AI-63 contextual follow-up flow:**
1. The client sends `session_id` with each question (`null` for a new chat). The API checks that the session belongs to the caller and to the requested class before starting the stream.
2. The last `CHAT_CONTEXT_EXCHANGES` complete Q&A pairs are loaded, bounded by `CHAT_CONTEXT_MAX_CHARS`. Orphaned messages from failed turns are ignored, and turns refused by the class scope gate are excluded (`chat_message.excluded_from_context`, migration `0024_chat_context_exclusion`).
3. The class scope gate and the follow-up rewrite start together and share `CHAT_PRE_RETRIEVAL_TIMEOUT_SECONDS`. The gate is awaited first: a refusal cancels the rewrite and streams immediately; otherwise the rewrite supplies the standalone retrieval question. If the rewrite fails or returns nothing, the question is qualified with the class name and description instead of being searched as a bare fragment. A gate that exceeds the deadline fails open.
4. The answer prompt receives the bounded history only for reference resolution. Previous assistant answers are never treated as evidence, and their old citation markers are stripped before the prompt is built.
5. The completed turn is persisted through `history_service.save_chat_turn()`. Assistant citations are stored in `chat_message.citations` (migration `0018_chat_message_citations`); a refused turn is stored with `excluded_from_context=True`.
6. The `done` SSE event returns `session_id`, `user_message_id`, and `assistant_message_id`. The frontend stores the session ID and refreshes the history sidebar.
7. Reloading a past conversation returns the original citations for each answer, so later turns cannot overwrite earlier citation lists.

With `LOG_LEVEL=INFO`, each turn logs `Chat turn started`, the gate/rewrite durations, `Scope gate blocked`, and `Pre-retrieval finished`, so slow or failing pre-retrieval steps are easy to spot.

Configuration:

| Setting | Default | Purpose |
|---|---:|---|
| `CHAT_CONTEXT_EXCHANGES` | `5` | Number of previous complete Q&A pairs sent to the model |
| `CHAT_CONTEXT_MAX_CHARS` | `6000` | Character budget for the conversation-history prompt |
| `CHAT_CLASS_CONTEXT_MAX_CHARS` | `240` | Class-description budget used to qualify a failed rewrite |
| `CHAT_PRE_RETRIEVAL_TIMEOUT_SECONDS` | `15` | Shared gate + rewrite deadline before retrieval starts |

See [`docs/design/ai-63-contextual-follow-up.md`](../../../docs/design/ai-63-contextual-follow-up.md) for the full design and test map.
