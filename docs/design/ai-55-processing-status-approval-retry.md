# AI-55 — Document Processing Status, Approval & Retry

> **Jira:** [AI-55 — Processing Status, Approval & Retry](https://ai03.atlassian.net/browse/AI-55)
> **Parent:** AI-45 — Document Ingestion Pipeline
> **Scope:** Backend document approval and ingestion lifecycle, plus its trainer-facing UI.

## 1. Purpose

AI-55 adds an approval gate before uploaded class material is processed into searchable Qdrant chunks. Trainers can see whether a document is awaiting approval, processing, indexed, rejected, or failed; failed documents can be retried with an actionable failure reason.

The implementation uses the shared document loader from AI-49/AI-51, Gemini embeddings, and the existing class-scoped Qdrant retrieval flow. A document is only added to the knowledge base after trainer approval.

## 2. Document lifecycle

```text
Upload → pending → Approve → processing → indexed
                         └──────────────→ failed → Retry → processing
                 └→ Reject → rejected
```

- Every upload starts as `pending`, including a replacement with the same filename. Extraction and embedding are not scheduled at upload time.
- A same-name upload in the same class receives the next integer `version`. The previous indexed version remains available while the new version waits for approval and processing.
- Approval changes the status to `processing` and schedules ingestion in a FastAPI background task.
- Ingestion resolves the stored source path, loads and cleans the document, creates searchable chunks with class/document metadata and locators, embeds them, and writes them to Qdrant.
- Successful ingestion changes the new version to `indexed`. Only then are older versions marked `replaced`, their vectors purged, and their source files removed.
- Processing errors change the status to `failed` and store a trainer-facing `failure_reason`. A failed replacement does not supersede the previous indexed version.
- Retrying is allowed only from `failed`; it clears the previous failure reason and re-runs ingestion.
- Rejection is allowed only from `pending`, changes the status to `rejected`, and does not schedule ingestion.

## 3. Implementation

### Backend

- `app/config/config.py` adds `DOCUMENT_STORAGE_DIR` (default `/app/storage/documents`). Docker Compose mounts persistent storage so original uploads survive container recreation; local development uses the configured local storage directory. Stored paths are resolved and checked against the storage root.
- `app/services/document_service.py` validates and persists uploads, assigns versions, enforces the status transitions, and handles source/vector cleanup for deletion and replacement.
- `app/routers/documents/router.py` provides approve, reject, and retry actions. Each checks authentication, assigned class access, and uploader ownership. Approval and retry schedule `ingestion_service.process_document_in_background` after the HTTP response returns.
- `app/services/ingestion_service.py` delegates extraction to the shared AI-49 document loader, cleans and chunks the text, adds locator and document metadata, embeds with Gemini, and stores chunks in Qdrant.
- Before writing chunks, ingestion purges existing vectors for that `document_id`; retries therefore replace that document's chunks instead of adding duplicates. If a document is deleted or cancelled during vector writing, the new vectors are purged where possible.
- On success, the service marks the document `indexed` and supersedes older versions. On failure, it removes partial vectors where possible, marks the document `failed`, and `_failure_reason` maps known failures to actionable messages: Gemini embedding quota, missing stored file, and Qdrant unavailability. Unknown failures get a short generic message; provider details remain in backend logs.
- `app/models/document.py` and migration `0013_document_failure_reason.py` persist the nullable `failure_reason` field. `DocumentRead` returns it to the UI.
- At startup, documents left in `processing` are marked `failed` with a retry message. This recovery assumes one backend instance; FastAPI `BackgroundTasks` is not a durable job queue.

### Frontend

- `features/documents/class-materials.tsx` displays document status and provides Approve/Reject for pending documents and Retry for failed documents.
- While any listed document is `processing`, the list polls every three seconds, including when pagination or a status filter would otherwise hide processing rows. Processing rows show three animated green dots.
- Failed rows show the failure reason in a tooltip and accessible text.
- `lib/documents.ts` calls the approval, rejection, and retry endpoints and maps `failure_reason` into the frontend document model.

## 4. API actions

All actions require a Bearer token and a trainer/admin account with access to the document's class. The uploader must perform the document action.

| Method and path | Allowed source status | Result |
|---|---|---|
| `POST /api/documents/{id}/approve` | `pending` | `processing`; schedules ingestion |
| `POST /api/documents/{id}/reject` | `pending` | `rejected`; no ingestion is scheduled |
| `POST /api/documents/{id}/retry` | `failed` | `processing`; clears failure reason and schedules ingestion |

Invalid status transitions return `400`; missing documents return `404`; unauthorized role, class, or uploader returns `403`.

## 5. Jira acceptance criteria

Jira describes the successful processing state as **Done**; the API and database represent that terminal state as `indexed`.

| Jira criterion | Implementation | Verification recorded |
|---|---|---|
| **AC-01:** A new upload starts as Pending approval; extraction does not start until approved. | Uploads start as `pending`; ingestion is scheduled by the approve action, not by upload. | Covered by document API tests. The manual PDF flow was uploaded, approved, and then indexed. |
| **AC-02:** The uploader approves their own document; a rejected file moves to Rejected and is never indexed. | Approval/rejection enforce uploader and class access. Reject only transitions `pending` to `rejected` and schedules no ingestion. | Approval/rejection and authorization cases are covered by document API tests. The final manual smoke test focused on approval/indexing. |
| **AC-03:** After approval, status moves to Processing, then Done after chunking and embedding, or Failed. | Status moves to `processing`, then `indexed` on success or `failed` with a failure reason on error. | Ingestion success/failure paths are covered by backend tests. A real PDF reached `indexed` in the manual smoke test. |
| **AC-04:** Retry appears on Failed and embedding is not duplicated (idempotent). | FE exposes Retry for failed records. Before re-indexing, the backend purges vectors for the same `document_id`; retry replaces its chunks. | Retry, vector purge, and ingestion behavior are covered by focused backend tests. |
| **AC-05:** An unapproved document is never queryable by the chatbot. | Upload and rejection do not create vectors; only approval schedules ingestion. Retrieval uses the class-scoped Qdrant collection. | Route tests verify upload/rejection do not schedule ingestion, and the manual Q&A smoke test used an approved, indexed PDF. A separate chatbot query test for pending/rejected records was not run. |

## 6. Verification record

- **Backend focused tests:** 27 passed for the Excel loader fallback, ingestion service, and Qdrant vector store.
- **Frontend tests:** `npm run test` — 48 passed across 5 test files, including 14 document tests.
- **Manual smoke test:** `The Water Cycle TG.pdf` was uploaded, approved, reached `indexed`, and produced a grounded chatbot answer with a source citation. The citation was rendered; opening the citation at the exact PDF location was not part of this AI-55 verification.
- **Not covered by that smoke test:** manual UI verification of rejection and retry after a controlled processing failure, and a fresh PostgreSQL database migration run.

## 7. Operational notes

- Processing uses FastAPI `BackgroundTasks`. If the backend restarts during processing, startup recovery marks the document failed so a trainer can retry. Multi-instance deployment would need durable jobs or processing leases; a persistent worker queue would be a separate reliability enhancement if required for deployment.
- Gemini quota exhaustion is reported as a friendly failure reason. Retrying while the provider quota is still exhausted fails again; retry after quota reset or configure a key with available quota.
- Only documents processed after approval have vectors in Qdrant; pending and rejected uploads are not ingested.
- A document in `indexed` status means its chunks were written successfully to Qdrant; to verify Q&A, ask a question whose answer appears in that document and confirm the response cites it.

## 8. Focused test commands

Run from `course-qa-agent-be`:

```powershell
python -m pytest --basetemp=.pytest-base-ai55-merge tests/services/ingestion/loader/test_excel_loader.py tests/services/ingestion/test_ingestion_service.py tests/services/vector_store/test_qdrant_store.py -q
uv run pytest tests/test_documents.py tests/services/ingestion/test_ingestion_service.py -q
uv run ruff check app/services/ingestion_service.py app/services/document_service.py app/routers/documents/router.py tests/test_documents.py tests/services/ingestion/test_ingestion_service.py
```

For frontend document tests, run from `course-qa-agent-fe`:

```powershell
npm run test
```

For a UI smoke check, upload a small supported document, approve it, observe `Processing` and the animated indicator, then confirm it becomes `Indexed`. To check failure handling, use a controlled provider failure and confirm the UI shows the failure reason and Retry action. Avoid deliberately exhausting a shared API quota.
