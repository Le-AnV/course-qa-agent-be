# AI-61 — Class-Scoped Document Q&A

> **Epic:** E4 — Contextual Q&A
>
> **Status:** Implemented
>
> **Branch:** `features/Design_class_document_QA`
>
> **Repositories:** `course-qa-agent-be`, `course-qa-agent-fe`

## 1. Scope and decisions

This task connects authenticated class chat to Gemini and Qdrant. Answers are grounded only in chunks retrieved for the requested class. The implementation does not call web search and returns an explicit material-gap response when no relevant chunks are available.

The implementation also persists uploaded source files so citations can open the original document after an authorization check.

Key decisions:

- `POST /api/chat` requires a Bearer token and a class UUID.
- Authorization is checked before embedding or retrieval.
- Qdrant uses cosine distance and `settings.retriever_top_k`.
- The Qdrant filter targets `metadata.class_id`, matching `langchain-qdrant` payload structure.
- Gemini receives the question and retrieved chunks only.
- Responses use JSON Server-Sent Events (SSE).
- The complete request has a configurable hard timeout, defaulting to 60 seconds.
- Source files are stored below `DOCUMENT_STORAGE_DIR`, backed by a Docker named volume.

## 2. Architecture and components

| Module | Responsibility |
|---|---|
| `app/schemas/chat.py` | Validates `{ message, class_id }` and rejects blank questions. |
| `app/routers/chat/router.py` | Authenticates the caller, checks class access, enforces the timeout, and streams SSE events. |
| `app/services/rag_service.py` | Retrieves class chunks, builds the grounded prompt, streams Gemini output, and maps chunk metadata to citations. |
| `app/services/embedding/gemini_embedding.py` | Creates 1536-dimensional Gemini embeddings with retry support and request options. |
| `app/services/vector_store/qdrant_store.py` | Creates a cosine collection and performs top-k similarity search with `metadata.class_id` filtering. |
| `app/services/document_service.py` | Stores uploaded files and resolves protected paths within the configured storage root. |
| `app/routers/documents/router.py` | Serves original files after class-level authorization. |
| `alembic/versions/0008_document_storage.py` | Adds nullable `document.storage_path` for existing database compatibility. |

## 3. Chat API contract

### 3.1. Request

`POST /api/chat`

Headers:

```http
Authorization: Bearer <access-token>
Content-Type: application/json
```

Body:

```json
{
  "message": "Explain recursion using the lecture notes.",
  "class_id": "7ca65e12-4211-4783-9999-1a2b3c4d5e6f"
}
```

### 3.2. SSE response

Successful responses use `Content-Type: text/event-stream`.

```text
event: token
data: {"text":"A recursive function ..."}

event: citation
data: {"marker":"[1]","document_id":"...","file_name":"Lecture 2.pdf","heading":"Recursion","page":4,"url":"/api/documents/.../content?page=4"}

event: done
data: {}
```

Generation and timeout failures after streaming starts use an `error` event and do not emit `done`:

```text
event: error
data: {"message":"The request timed out. Please try again."}
```

### 3.3. HTTP errors

| Status | Condition |
|---|---|
| `401` | Bearer token is missing or invalid. |
| `403` | The caller is not an active member or owner of the class. |
| `404` | The requested class does not exist. |
| `422` | The request body, class UUID, or message is invalid. |
| `503` | Embedding or Qdrant retrieval is unavailable. |
| `504` | Retrieval exceeds the configured request deadline. |

## 4. Citation and file contract

Each indexed chunk used by chat must contain:

| Metadata | Required | Purpose |
|---|---|---|
| `class_id` | Yes | Qdrant class isolation. |
| `document_id` | Yes | Builds the protected source URL. |
| `file_name` | Yes | Citation label and file type fallback. |
| `chapter` or `headers` | Recommended | Displays the source section. |
| `page_number` or `page` | Recommended | Opens a PDF at the cited page. |
| `file_type` | Recommended | Detects PDF citations. |

Original files are served through:

```http
GET /api/documents/{document_id}/content
Authorization: Bearer <access-token>
```

Admins may access all class documents. Trainers may access documents from their assigned classes. Students and teaching assistants require an active enrollment.

## 5. Configuration and storage

```env
DOCUMENT_STORAGE_DIR=data/documents
CHAT_TIMEOUT_SECONDS=60
RETRIEVER_TOP_K=3
GOOGLE_API_KEY=<secret>
```

Docker mounts the `course-qa-agent_document_storage` volume at `/app/data/documents`.

Apply the schema migration after updating the backend image:

```bash
docker compose up -d --build backend
docker compose exec backend alembic upgrade head
```

## 6. Acceptance criteria status

| Requirement | Status | Evidence |
|---|---|---|
| Authenticate and authorize before retrieval | Implemented | Router tests cover missing/invalid tokens, permitted enrollment, and forbidden class access. |
| Retrieve top-k chunks from the requested class only | Implemented | Qdrant unit test asserts `metadata.class_id` and configured top-k behavior. |
| Generate only from retrieved course context | Implemented | System instruction prohibits outside knowledge and no web tool is configured. |
| Return a material-gap answer when no chunk exists | Implemented | Service returns without invoking Gemini. |
| Stream answer, citations, completion, and errors | Implemented | Router and frontend parser tests cover the JSON SSE contract. |
| Open protected source files and PDF pages | Implemented | Document tests cover stored bytes, member access, and outsider rejection. |
| Complete within 60 seconds | Implemented as a hard timeout | Average latency below 30 seconds requires live Gemini and Qdrant measurement. |

## 7. Verification

Run backend tests without the two live Gemini network tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider `
  tests/chat tests/services/chat `
  tests/services/vector_store/test_qdrant_store.py `
  tests/services/embedding/test_gemini_embedding.py `
  tests/test_documents.py -q `
  -k "not test_gemini_embedding_real_api_documents and not test_gemini_embedding_real_api_query"
```

Run lint checks:

```powershell
.\.venv\Scripts\ruff.exe check app tests/chat tests/services/chat `
  tests/services/vector_store/test_qdrant_store.py tests/test_documents.py
```

Verified during implementation:

- 46 backend tests passed; 2 live Gemini tests were deselected.
- 25 focused chat, RAG, and Qdrant tests passed after the final class-filter correction.
- Ruff passed.
- `docker compose config --quiet` passed.

## 8. Integration dependency

End-to-end retrieval requires the AI-55 indexing pipeline to create Qdrant chunks with the metadata in section 4. Until those points and corresponding source files exist, the endpoint can be verified with mocked retrieval but cannot complete the real upload-to-answer acceptance path.

Chat history persistence and contextual follow-up questions remain separate tasks.
