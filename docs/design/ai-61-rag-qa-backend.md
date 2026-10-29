# AI-61 — RAG Q&A Backend

> **Task:** AI-61 · RAG Q&A Backend
>
> **Design branch:** `features/Design_RAG_QA_Backend`
>
> **Implementation reference:** `features/Implementation_RAG_QA_Backend`
>
> **Related tasks:** AI-39 (document lifecycle), AI-49 (document extraction), AI-65 (retrieval threshold and source routing), AI-67 (Internet source agent), AI-69 (chat processing status)

## Purpose

Let an authenticated student ask a question in a class chat and receive a streamed answer grounded in class materials. Before retrieval, the backend rejects questions that are confidently outside the class subject (class scope gate). For questions that pass it, the backend resolves follow-ups, retrieves and reranks class passages, routes between class documents and Internet sources using retrieval scores, and streams the answer with citations.

## Scope

The implementation includes class-scoped retrieval, Cross Encoder reranking, grounded answer generation, SSE progress and answer events, citations, and access to cited source files. It also checks class scope before retrieval and uses the AI-65 score thresholds to choose between class materials, Internet search, and no-evidence responses. AI-67 provides the SerpApi Internet search integration.

## Request flow

```text
Student
  │ POST /api/chat { message, class_id }
  ▼
Authenticate and verify membership in the requested class
  ▼
Return a deterministic quick response when applicable
  ▼
Class scope check against the class name and description (Gemini by default)
  ├─ confidently out of scope (confidence >= threshold) → refuse; skip retrieval and web search
  └─ in scope, uncertain, or decision unavailable → continue
  ▼
Resolve follow-up question using bounded conversation history (when applicable)
  ▼
Retrieve up to 10 candidate passages from Qdrant
  ├─ filter by class_id
  └─ keep passages belonging to indexed, live documents
  ▼
If best cosine score < T_low (default 0.45), skip Cross Encoder and return no-evidence response
  ▼
Cross Encoder reranks candidates and keeps the top 3 by default (`RERANK_TOP_K`)
  ├─ score >= T_high (DB `retrieval_threshold`, currently 0.7; code default 0.80) → answer from class documents
  ├─ T_low <= score < T_high → search the Internet with SerpApi
  └─ no candidates / score < T_low → return no-evidence response
  ▼
Stream a grounded Gemini answer and citations as SSE
```

The class gate only stops a question when the decision service returns `out_of_scope` at or above the configured confidence. If the key is absent, the response is uncertain, or the request fails, the service continues to RAG. When retrieval has no candidates or falls below the lower threshold, the chat service returns a fixed no-evidence message instead of asking Gemini to answer without evidence.

## Class scope gate

`is_confidently_out_of_scope()` sends the class name, optional class description, current question, and bounded recent conversation to the configured decision provider with `DECISION_MODEL`. Gemini is the default (`DECISION_PROVIDER=gemini`, native `google-genai` structured output using a Gemini key).  In both cases the model answers with a JSON `verdict` (`in_scope` or `out_of_scope`) and `confidence`. Recent conversation is included to resolve short follow-up wording; it does not broaden the class subject. Credentials stay server-side and are never returned to the client.

Keys come from the admin-managed pools first (`gemini_api_keys`, managed at `/admin/settings`); `GEMINI_API_KEY` in the backend `.env` are only used when no pool key is healthy. `DECISION_KEY_ATTEMPTS` (3) bounds how many pool keys one attempt may try in priority order. A rejected key is marked invalid, a rate-limited or failing key cools down, and the next healthy key is tried. The gate has its own model-level fallback and cooldown (see below) on top of key rotation.

`DECISION_TIMEOUT_SECONDS` (12s), `DECISION_MAX_OUTPUT_TOKENS` (200) bound the call. Only an `out_of_scope` verdict with confidence `>= DECISION_CONFIDENCE_THRESHOLD` (0.8) stops processing. Missing credentials, provider errors, malformed payloads, unknown verdicts, and lower-confidence results fail open and continue through the existing retrieval flow. A confident rejection returns a fixed out-of-scope answer before document retrieval or Internet search.

A transient Gemini `503 UNAVAILABLE` or `504 DEADLINE_EXCEEDED` must not disable the gate for every turn. When `DECISION_FALLBACK_ENABLED=true` (default), the primary attempt uses the budget left after reserving `DECISION_FALLBACK_TIMEOUT_SECONDS` (6s) inside `DECISION_TIMEOUT_SECONDS`; if it fails, `DECISION_FALLBACK_MODEL` (`models/gemini-flash-lite-latest`) is tried once with the reserved budget. A failing primary is then skipped for `DECISION_COOLDOWN_SECONDS` (60s), so later turns call the fallback directly instead of paying the same timeout again. Both attempts failing still fail open. The Gemini SDK enforces a 10s minimum manual HTTP deadline, so the transport deadline is never set below that; `asyncio.wait_for` still enforces the shorter per-attempt budget.

## Retrieval and reranking

1. `retrieve_class_chunks()` performs scored vector search with the requested `class_id` filter.
2. The retrieval service applies the document lifecycle guard so deleted or replaced documents cannot be used as evidence.
3. If the best vector score is below the lower threshold, the service skips Cross Encoder inference and returns the no-evidence route.
4. Otherwise, the Cross Encoder evaluates each question/passage pair and orders the candidates by its output logits; the three highest-ranked passages are retained by default (`RERANK_TOP_K` can override this value).
5. The retained passages keep their original vector similarity scores. The highest retained vector score is compared with the configured high and low thresholds to select the source route.

The current constants are 10 retrieval candidates, three retained passages by default (`RERANK_TOP_K`), and a maximum model sequence length of 512 tokens. The model is configured by `RERANKER_MODEL`, defaulting to `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`. It is loaded lazily once per backend process, runs on CUDA when available and otherwise on CPU, and is set to evaluation mode. Docker stores the Hugging Face cache in the `reranker_cache` volume mounted at `/app/model-cache`. Cross Encoder logits rank passages; they are not the threshold score.

## API

### `POST /api/chat`

Requires a bearer-authenticated user who is an active student in the class, a trainer assigned to it, or an admin. Archived classes are hidden from non-admins. A non-member receives `403`; an unknown or inaccessible archived class receives `404`.

Request body:

```json
{
  "message": "What are the main stages of the water cycle?",
  "class_id": "<class UUID>"
}
```

`message` is trimmed and must not be blank. The response uses `text/event-stream` and disables proxy buffering.

| Event | Data | Meaning |
|---|---|---|
| `progress` | `{ "stage": "searching", "message": "Checking class scope" }` | Class scope decision started. |
| `progress` | `{ "stage": "searching", "message": "Searching class materials" }` | Class retrieval and reranking started. |
| `progress` | `{ "stage": "reading", "message": "Found ..." }` | Relevant class passages are available. |
| `progress` | `{ "stage": "searching", "message": "Searching the web" }` | AI-65 routing selected Internet search. |
| `progress` | `{ "stage": "reading", "message": "Reviewing ... web sources" }` | Internet results are available. |
| `progress` | `{ "stage": "drafting", "message": "Synthesizing answer" }` | Grounded answer generation started. |
| `token` | `{ "text": "..." }` | A piece of the answer, or the deterministic no-evidence message. |
| `citation` | A serialized source object | Source used for the generated answer. Includes title, snippet and document ID; page is included when available. |
| `suggestions` | `{ "questions": ["..."] }` | Optional grounded follow-up questions after a completed class-material answer. |
| `done` | `{ "source": "curriculum", "used_web_search": false, "threshold": 0.8, "top_score": ..., "suggestions": [...] }` | Stream completed. `source` can be `conversation`, `curriculum`, `internet`, `unrelated`, or `none`. `top_score` is the retained vector similarity score, not the Cross Encoder ranking logit. |
| `error` | `{ "message": "..." }` | A safe generation or timeout message. |

The chat service timeout is `CHAT_TIMEOUT_SECONDS` (300s by default). The stream reports a timeout message if retrieval/reranking or answer generation exceeds it, and a generic message if an unexpected error prevents completion.

### `GET /api/chat/sources/{document_id}`

Opens the cited source file inline. The document must still exist with `indexed` status, and the caller must have access to its class. Missing, non-indexed, or unavailable source files return `404`.

## Grounding and citations

The service numbers the retained passages and sends them with the student's question to `answer-course-prompt`. The prompt directs the model to answer from supplied course material, preserve the student's language, avoid unsupported claims, and use the supplied citation markers. Citation records are created from the retained passages and include the document title, excerpt, document ID, and page locator where available.

When there are no valid citations, the service skips answer generation. Retrieval errors, model errors, and timeouts are surfaced through the SSE `error` event rather than returning an uncited model answer.

## Current limitations

- The scope gate uses class name and description; it does not inspect retrieved passages or decide whether those passages fully answer the question. Missing or failed decisions continue to RAG.
- AI-65 routing uses vector cosine scores, not Cross Encoder logits. With the current thresholds (`unrelated_threshold` 0.45, `retrieval_threshold` 0.7 in `system_config`), a score from 0.45 up to (but not including) 0.7 routes to Internet search.
- If class passages are selected but do not answer the question, the answer prompt is responsible for abstaining. There is no separate deterministic evidence evaluator in this flow.
- Off-topic turns are refused before retrieval, so they are not sent to the retrieval or web search path. The class gate is stateless: every question is compared with the class name and description, so a session cannot drift into another subject without being checked.
- Internet search requires a configured `SERPAPI_API_KEY`; if no web results are available, the service returns a fixed no-source message.
- Reranking runs locally in the backend process. On CPU, cold model loading and pair scoring can add latency; the persistent model cache avoids downloading model files on every container start, but does not eliminate first-load inference cost.

## Implementation map

| Concern | Implementation reference |
|---|---|
| Request validation | `app/schemas/chat.py` |
| Chat and cited-file routes; class access checks | `app/routers/chat/router.py` |
| Class-scoped retrieval and reranking orchestration | `app/services/chat_retrieval_service.py` |
| Cross Encoder loading and scoring | `app/services/reranker_service.py` |
| SSE orchestration, prompt formatting, generation and citations | `app/services/chat_service.py` |
| Pre-retrieval class scope gate | `app/services/decision_service.py`, key pools via `app/services/gemini_key_service.py`, `GEMINI_API_KEY` fallback in `app/config/config.py` |
| Scope decision prompt | `app/config/prompts/class_scope.yaml` (`class-scope-prompt`) |
| Scope decision configuration | `DECISION_*` settings in `app/config/config.py` and `.env.example` |
| Qdrant retrieval and live-document guard | `app/services/rag_service.py` and `app/services/vector_store/qdrant_store.py` |
| Prompt contract | `app/config/prompts/answer_course.yaml` (`answer-course-prompt`) |
| Reranker model configuration | `app/config/config.py`, `.env.example` |
| Chat timeout | `CHAT_TIMEOUT_SECONDS` in `app/config/config.py` |
| Persistent model download cache | `docker-compose.yml` (`reranker_cache`) |

## Verification

Run the targeted backend tests and lint checks from the backend repository:

```powershell
uv run python -m pytest tests/chat/ tests/services/chat/ tests/services/rag/ -q
uv run ruff check .
```

For a manual check, start the backend with PostgreSQL and Qdrant available, sign in as a class member, and submit a question whose answer is present in an indexed class document. Confirm the response is streamed, citations point to that class's documents, opening a citation serves the source file, and a question with no matching evidence receives the no-evidence message without an LLM-generated answer.

For the class scope gate, add at least one key under Settings > Gemini key pool or configure `GEMINI_API_KEY`, then test a clearly in-scope question and a clearly out-of-scope question. Confirm the latter receives the fixed scope response and does not emit `Searching class materials` or `Searching the web`. Also verify that with no healthy key and no environment fallback, or an unavailable provider, the existing RAG flow continues. Follow-up suggestions are emitted only after a grounded answer from class documents.
