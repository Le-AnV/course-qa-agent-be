# AI-61 — RAG Q&A Backend

> **Task:** AI-61 · RAG Q&A Backend
>
> **Design branch:** `features/Design_RAG_QA_Backend`
>
> **Implementation reference:** `features/Implementation_RAG_QA_Backend`
>
> **Related tasks:** AI-39 (document lifecycle), AI-49 (document extraction), AI-65 (retrieval threshold and source routing), AI-67 (Internet source agent), AI-69 (chat processing status)

## Purpose

Let an authenticated student ask a question in a class chat and receive a streamed answer grounded in that class's indexed course documents. The backend keeps retrieval within the requested class, reranks retrieved passages, generates an answer from the selected evidence, and streams citations that point to the source document and page when available.

## Scope

AI-61 covers class-scoped document retrieval, Cross Encoder reranking, grounded answer generation, SSE progress and answer events, citations, and access to cited source files.

The chat endpoint does not perform Internet search. AI-67 owns the Internet search provider. AI-65 owns source-routing behavior in the RAG service; its threshold value is included in chat completion metadata, but the AI-61 streaming retrieval path does not currently use that value as a minimum-evidence cutoff. See [Current limitations](#current-limitations).

## Request flow

```text
Student
  │ POST /api/chat { message, class_id }
  ▼
Authenticate and verify membership in the requested class
  ▼
Retrieve up to 20 candidate passages from Qdrant
  ├─ filter by class_id
  └─ keep passages belonging to indexed, live documents
  ▼
Cross Encoder reranks candidates and keeps the top 5
  ▼
Build numbered context and apply answer-course-prompt
  ▼
Stream Gemini answer tokens and citations as SSE
```

If retrieval or reranking produces no usable passage, the service returns a fixed message asking the student to rephrase or ask the trainer to add material. It does not ask the LLM to answer without evidence.

## Retrieval and reranking

1. `retrieve_class_chunks()` performs scored vector search with the requested `class_id` filter.
2. The retrieval service applies the document lifecycle guard so deleted or replaced documents cannot be used as evidence.
3. The Cross Encoder evaluates each question/passage pair and orders the candidates by its output logits.
4. The five highest-ranked passages are passed to answer generation.

The current constants are 20 retrieval candidates, five retained passages, and a maximum model sequence length of 512 tokens. The model is configured by `RERANKER_MODEL`, defaulting to `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`. It is loaded lazily once per backend process, runs on CUDA when available and otherwise on CPU, and is set to evaluation mode. Docker stores the Hugging Face cache in the `reranker_cache` volume mounted at `/app/model-cache`.

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
| `progress` | `{ "stage": "searching" }` | Class retrieval and reranking started. |
| `progress` | `{ "stage": "reading" }` | At least one passage is available. |
| `progress` | `{ "stage": "drafting" }` | Grounded answer generation started. |
| `token` | `{ "text": "..." }` | A piece of the answer, or the deterministic no-evidence message. |
| `citation` | A serialized source object | Source used for the generated answer. Includes title, snippet and document ID; page is included when available. |
| `done` | `{ "source": "curriculum", "used_web_search": false, "threshold": 0.8, "top_score": ... }` | Stream completed. `source` is `none` when there are no usable passages. `top_score` is the retained vector similarity score, not the Cross Encoder ranking logit. |
| `error` | `{ "message": "..." }` | A safe generation or timeout message. |

The chat service timeout constant is 300 seconds. The stream reports a timeout message if retrieval/reranking or answer generation exceeds it, and a generic message if an unexpected error prevents completion.

### `GET /api/chat/sources/{document_id}`

Opens the cited source file inline. The document must still exist with `indexed` status, and the caller must have access to its class. Missing, non-indexed, or unavailable source files return `404`.

## Grounding and citations

The service numbers the retained passages and sends them with the student's question to `answer-course-prompt`. The prompt directs the model to answer from supplied course material, preserve the student's language, avoid unsupported claims, and use the supplied citation markers. Citation records are created from the retained passages and include the document title, excerpt, document ID, and page locator where available.

When there are no valid citations, the service skips answer generation. Retrieval errors, model errors, and timeouts are surfaced through the SSE `error` event rather than returning an uncited model answer.

## Current limitations

- Cross Encoder logits are used to rank passages; they are not calibrated confidence percentages and must not be interpreted as an “80% confidence” score.
- The streaming chat path reads the configured AI-65 retrieval threshold for response metadata, but does not currently reject evidence based on that threshold. A non-empty reranked result is treated as curriculum evidence. An explicit evidence-sufficiency evaluator or threshold policy would need a separate implementation and tests.
- If passages are present but do not answer the question, the model prompt is responsible for abstaining. There is no separate deterministic evidence evaluator in this flow.
- Internet fallback is outside this endpoint until AI-67 is integrated into the streaming path.
- Reranking runs locally in the backend process. On CPU, cold model loading and pair scoring can add latency; the persistent model cache avoids downloading model files on every container start, but does not eliminate first-load inference cost.

## Implementation map

| Concern | Implementation reference |
|---|---|
| Request validation | `app/schemas/chat.py` |
| Chat and cited-file routes; class access checks | `app/routers/chat/router.py` |
| Class-scoped retrieval and reranking orchestration | `app/services/chat_retrieval_service.py` |
| Cross Encoder loading and scoring | `app/services/reranker_service.py` |
| SSE orchestration, prompt formatting, generation and citations | `app/services/chat_service.py` |
| Qdrant retrieval and live-document guard | `app/services/rag_service.py` and `app/services/vector_store/qdrant_store.py` |
| Prompt contract | `app/config/prompt.yaml` (`answer-course-prompt`) |
| Reranker model configuration | `app/config/config.py`, `.env.example` |
| Chat timeout | `CHAT_TIMEOUT_SECONDS` in `app/services/chat_service.py` |
| Persistent model download cache | `docker-compose.yml` (`reranker_cache`) |

## Verification

Run the targeted backend tests and lint checks from the backend repository:

```powershell
uv run python -m pytest tests/chat/ tests/services/chat/ tests/services/rag/ -q
uv run ruff check .
```

For a manual check, start the backend with PostgreSQL and Qdrant available, sign in as a class member, and submit a question whose answer is present in an indexed class document. Confirm the response is streamed, citations point to that class's documents, opening a citation serves the source file, and a question with no matching evidence receives the no-evidence message without an LLM-generated answer.
