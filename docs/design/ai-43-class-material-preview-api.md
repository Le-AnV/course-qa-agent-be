# AI-43 — Class material preview and download (API design)

**Status:** Draft for review
**Related:** AI-35 (upload), AI-37 (document list), AI-39 (delete/replace), AI-41 (class tags), AI-43 (this task)
**Implementation branch:** `features/Implementation_TicketEscalationAndPreview`

---

## 1. Scope

AI-43 lets an authorized trainer read an uploaded class material without exposing the
storage directory or a public object URL.

In scope:

- Download the original uploaded bytes as an attachment.
- Preview PDFs and images inline in the browser.
- Reuse the existing trainer/admin authentication and class-assignment scope.
- Return a clear error when a file type cannot be previewed or the stored file is
  missing.

Out of scope:

- Editing or converting materials.
- Generating thumbnails.
- Public or signed share links.
- Student access to trainer materials.
- Changes to the upload allow-list.

---

## 2. Actors and permissions

| Actor | Download | Preview |
|---|---|---|
| Trainer assigned to the document's class | Yes | Yes, PDF/image |
| Co-trainer assigned to the class | Yes | Yes, PDF/image |
| Trainer not assigned to the class | `403` | `403` |
| Student | `403` | `403` |
| Admin authenticated through `require_trainer` | Subject to the same class scope | Subject to the same class scope |
| Unauthenticated | `401` | `401` |

Both endpoints use the existing `require_trainer` dependency and the existing class
scope helper. The client never receives a filesystem path.

---

## 3. API contract

Base path: `/api/documents`

### 3.1 `GET /api/documents/{document_id}/download`

Returns the original file as an attachment.

| Item | Value |
|---|---|
| Success | `200 OK` |
| Body | Original uploaded bytes |
| `Content-Type` | Stored MIME type, fallback `application/octet-stream` |
| `Content-Disposition` | `attachment; filename="<original file name>"` |
| Missing document | `404` |
| Missing file on disk | `404` with a generic message; never expose the path |
| Class not assigned | `403` |
| Invalid/expired token | `401` |

### 3.2 `GET /api/documents/{document_id}/preview`

Returns the original file for inline browser rendering when the MIME type is safe.

| Item | Value |
|---|---|
| Allowed MIME types | `application/pdf`, `image/*` |
| Success | `200 OK` |
| Body | Original uploaded bytes |
| `Content-Type` | Stored MIME type |
| `Content-Disposition` | `inline; filename="<original file name>"` |
| Unsupported type | `415` with “This file type cannot be previewed; download it instead.” |
| Missing document/file | `404` |
| Class not assigned | `403` |
| Invalid/expired token | `401` |

`text/plain`, Markdown, DOCX, PPTX, and XLSX are intentionally not previewed inline;
they use the download endpoint. This avoids rendering untrusted HTML or relying on
browser-specific document viewers.

### 3.3 Example

```http
GET /api/documents/3f0f2d1e-8d8f-4f3a-9f0e-1c2b3a4d5e6f/download
Authorization: Bearer <trainer-token>

HTTP/1.1 200 OK
Content-Type: application/pdf
Content-Disposition: attachment; filename="cpp_tutorial.pdf"
```

---

## 4. Security and storage behavior

- The document row is loaded first; its `class_id` must be in the caller's assigned
  class IDs.
- The stored path is resolved from the document record, never from a request path or
  file name.
- The file must exist and be a regular file before it is served.
- The endpoint does not redirect to object storage and does not create a public URL.
- The browser receives the file only after the Bearer-authenticated request succeeds.
- The original filename is used in `Content-Disposition`; the backend does not expose
  the storage path.
- A missing row and a missing on-disk file both return `404`; the detail text does not
  reveal server paths.

---

## 5. Error model

| Status | Case |
|---|---|
| `401` | Missing, invalid, or expired access token. |
| `403` | Student, unassigned trainer, or class outside the caller's scope. |
| `404` | Document row not found, or stored file missing. |
| `415` | Preview requested for a non-PDF/non-image MIME type. |
| `422` | Malformed document ID rejected by request validation, where applicable. |

Download never returns `415`; unsupported preview types are still downloadable.

---

## 6. AC mapping

| Acceptance criterion | Implementation |
|---|---|
| AC-01: preview a class material | `GET /{id}/preview`; inline PDF/image. |
| AC-02: download the original file | `GET /{id}/download`; attachment disposition and original bytes. |
| AC-03: only authorized trainers can access | `require_trainer` plus class scope on both endpoints. |
| Unsupported type | `415` on preview; download remains available. |
| Missing file | `404` without leaking the storage path. |

---

## 7. Verification plan

- Upload a PDF, fetch `/download`, assert exact bytes, MIME type, and `attachment`
  disposition.
- Fetch `/preview` for the same PDF and assert `inline` disposition.
- Set a record to `image/png` and assert inline preview.
- Upload `text/plain` and assert preview `415`, download `200`.
- Request without a token, as a student, and as an unassigned trainer; assert
  `401`/`403`.
- Request an unknown document ID and a deleted on-disk file; assert `404`.
- Confirm the UI receives no public URL and sends the Bearer token on both requests.
- Run `uv run --no-sync python -m pytest tests/test_documents.py -q`.

---

## 8. Open decisions

1. **Range requests:** the current contract does not promise partial-content support.
   Large PDFs rely on the browser's handling of the full response.
2. **Inline images:** the API supports `image/*`, while the current upload allow-list
   is document-oriented. The preview branch is still correct for legacy/imported image
   rows.
3. **Audit logging:** file access is authorized but not separately audited in AI-43.
