# AI-55 — Document Processing Status, Approval & Retry

> **Ticket:** AI-55
> **Status:** Implemented; successful real-provider indexing still needs end-to-end verification.

## 1. Purpose

After a trainer uploads a class document, the system must retain the original file, let the trainer approve or reject it, process approved files into searchable chunks, and show whether processing is still running or has failed. A trainer must be able to see the failure reason and retry a failed document.

This connects the existing upload and document parsing work (AI-35/AI-51) to the embedding and Qdrant retrieval flow (AI-53). The Q&A feature can use the document only after its chunks have been indexed.

## 2. Document lifecycle

```text
Upload → pending → Approve → processing → indexed
                         └──────────────→ failed → Retry → processing
                 └→ Reject → rejected
```

- New uploads are stored on disk and start as `pending`.
- Approval changes the status to `processing` and schedules background ingestion.
- Ingestion parses the stored file, cleans and chunks its text, attaches document/class metadata, embeds the chunks, and writes them to Qdrant.
- Successful indexing changes the status to `indexed`.
- Processing errors change the status to `failed` and store a trainer-facing `failure_reason`.
- Retrying a failed document clears its previous reason, returns it to `processing`, and schedules ingestion again.
- Rejection is only allowed from `pending` and ends in `rejected`.

## 3. Implementation

### Backend

- `app/config/config.py` adds `DOCUMENT_STORAGE_DIR` (default `/app/storage/documents`). `docker-compose.yml` mounts persistent storage so original uploads survive container recreation.
- `app/services/document_service.py` stores uploaded bytes and implements the status transitions, including clearing the previous failure reason on approval/retry.
- `app/routers/documents/router.py` provides the approval, rejection, and retry actions. Each checks authentication, assigned class access, and uploader ownership. Approval and retry schedule `ingestion_service.process_document_in_background` after returning the HTTP response.
- `app/services/ingestion_service.py` performs parsing, cleaning, chunking, embedding, and Qdrant writes. It marks successful documents `indexed`; on failure it cleans up partial vectors where possible, marks the document `failed`, and logs the exception.
- `_failure_reason` maps known failures to actionable messages, including Gemini embedding quota, missing stored file, and Qdrant unavailability. Unknown failures get a short generic message; technical details stay in backend logs.
- `app/models/document.py` and migration `0013_document_failure_reason.py` persist the nullable `failure_reason` field. `DocumentRead` returns it to the UI.

### Frontend

- `features/documents/class-materials.tsx` presents document status and exposes Approve/Reject for pending documents and Retry for failed documents.
- While any listed document is `processing`, the list refreshes every three seconds. Processing rows show three animated green dots.
- Failed rows show the failure reason in the status tooltip and accessible text, and provide a Retry action.
- `lib/documents.ts` calls the approval, rejection, and retry endpoints and maps `failure_reason` into the frontend document model.

## 4. API actions

All actions require a Bearer token and a trainer/admin account with access to the document's class; the uploader must perform the document action.

| Method and path | Allowed source status | Result |
|---|---|---|
| `POST /api/documents/{id}/approve` | `pending` | `processing`; schedules ingestion |
| `POST /api/documents/{id}/reject` | `pending` | `rejected` |
| `POST /api/documents/{id}/retry` | `failed` | `processing`, clears `failure_reason`, schedules ingestion |

Invalid transitions return `400`; missing documents return `404`; unauthorized role, class, or uploader returns `403`.

## 5. Acceptance criteria review

The Jira acceptance criteria were not available in the repository during this review. This table maps the implemented behavior to the AI-55 scope discussed by the team.

| Behavior | Implementation status | Evidence / limitation |
|---|---|---|
| Uploaded file remains available for processing | Implemented | Configurable storage path and Docker persistent volume |
| Trainer approval starts document processing | Implemented | Approve endpoint schedules background ingestion |
| Processing result is reflected as `indexed` or `failed` | Implemented | Ingestion service persists terminal status |
| Failure reason is available to the trainer | Implemented | API schema and FE failed-status tooltip/text |
| Failed processing can be retried | Implemented | Retry endpoint returns to `processing` and reruns ingestion |
| UI indicates active processing and refreshes status | Implemented | Animated dots and three-second polling |
| Real document successfully reaches `indexed` against Gemini and Qdrant | Not verified end-to-end | Recent local attempt failed because Gemini embedding quota was exhausted; retry after quota reset or with a usable key |

Automated tests for approval/retry, ingestion success/failure, and quota-specific failure messaging are present in `tests/test_documents.py` and `tests/services/ingestion/test_ingestion_service.py`. They were not run as part of this documentation review.

## 6. Operational notes and limits

- Processing currently uses FastAPI `BackgroundTasks` in the backend process. It is suitable for the current local flow, but it is not a durable job queue: a process/container restart can interrupt an in-flight job. A persistent worker queue would be a separate reliability enhancement if required for deployment.
- Gemini quota errors are recorded as a friendly failure reason. Retrying while the provider quota is still exhausted will fail again; retry after quota reset or configure a key with available quota.
- A document in `indexed` status means its chunks were written successfully to Qdrant. To verify Q&A, ask a question whose answer appears in that document and confirm the response cites it.

## 7. Verification commands

Run from `course-qa-agent-be`:

```powershell
uv run pytest tests/test_documents.py tests/services/ingestion/test_ingestion_service.py -q
uv run ruff check app/services/ingestion_service.py app/services/document_service.py app/routers/documents/router.py tests/test_documents.py tests/services/ingestion/test_ingestion_service.py
```

For a UI smoke check, upload a small supported document, approve it, observe `Processing` and the animated indicator, then confirm it becomes `Indexed`. To check failure handling, use a controlled provider failure and confirm the UI shows the failure reason and Retry action. Avoid deliberately exhausting a shared API quota.
