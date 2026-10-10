# AI Course Syllabus & Q&A Agent — Core ERD

> **Canonical ERD:** This is the only ERD in the project. All user stories, migrations, backend models, and API implementations must reference this document instead of defining separate ERDs.
>
> **Path:** `docs/design/core-erd.md`

## Conventions

- Database: PostgreSQL.
- Table names are singular and must match migrations exactly.
- `PK` = primary key; `FK` = foreign key; `UK` = unique constraint; `IDX` = indexed column.
- Passwords are never stored in plaintext. `account` contains `password_hash` only.
- No self-registration: accounts are provisioned through administrator-managed bulk import.
- Qdrant stores vector embeddings; PostgreSQL stores relational data, document metadata, and chunk text.
- FastAPI must enforce class-level authorization; foreign keys alone do not enforce access policies.

## Table responsibilities

| Table | Purpose |
|---|---|
| `user` | User profile and assigned role |
| `account` | Authentication credentials; only `password_hash` is stored |
| `role` | Application roles |
| `permission` | Authorization capabilities |
| `role_permission` | Role-to-permission mapping |
| `course` | Course catalogue |
| `class` | Course offering assigned to one trainer |
| `enrollment` | Student membership in a class |
| `document` | Uploaded file metadata and approval/processing states |
| `document_chunk` | Extracted text chunks and metadata for retrieval/citations |
| `chat_session` | A user's conversation session |
| `chat_message` | User, assistant, or system message |
| `citation` | Connects a chat message to a document chunk |
| `ticket` | Support request, optionally associated with a class |
| `ticket_reply` | Replies on a ticket |
| `app_config` | Application-wide key/value configuration |

## Seeded roles

- `STUDENT`
- `TEACHING_ASSISTANT`
- `TRAINER`
- `ADMIN`

`TEACHING_ASSISTANT` is included as a role, but its permissions should remain unassigned until the team approves its responsibilities. Seed values must match the migration exactly.

## Status values

| Column | Allowed values |
|---|---|
| `user.status` | `ACTIVE`, `LOCKED`, `INACTIVE` |
| `course.status` | `ACTIVE`, `INACTIVE` |
| `class.status` | `ACTIVE`, `LOCKED`, `INACTIVE` |
| `enrollment.status` | `ACTIVE`, `INACTIVE` |
| `document.approval_status` | `PENDING`, `APPROVED`, `REJECTED` |
| `document.processing_status` | `NOT_STARTED`, `PROCESSING`, `DONE`, `FAILED` |
| `document_chunk.embedding_status` | `NOT_STARTED`, `PROCESSING`, `DONE`, `FAILED` |
| `ticket.status` | `OPEN`, `IN_PROGRESS`, `RESOLVED`, `CLOSED` |
| `chat_message.role` | `USER`, `ASSISTANT`, `SYSTEM` |

Initial supported file types: `PDF`, `DOCX`, `MD`, `TXT`, `PPT`, `PPTX`.

## Constraints and indexes

### Uniqueness

- `user.email`, `role.name`, `permission.name`
- `account.user_id` — one account per user
- `course.code`, `class.code`
- `enrollment(class_id, student_id)` — prevent duplicate enrollment
- `role_permission(role_id, permission_id)` — prevent duplicate role-permission assignments
- `citation(message_id, document_chunk_id)` — prevent duplicate citation of a chunk by the same message

### Indexed columns

The schema includes indexes for the following important access paths:

- `enrollment.class_id`
- `document.class_id`
- `document_chunk.document_id`
- `document_chunk.class_id`
- `chat_message.session_id`

Additional indexes cover frequently queried foreign keys such as `class.course_id`, `class.trainer_id`, `enrollment.student_id`, `chat_session.user_id`, and ticket relationships.

## Class-level authorization rules

1. A course can have multiple classes.
2. Each class references one course and one trainer through `class.course_id` and `class.trainer_id`.
3. Students join classes through `enrollment`.
4. Documents belong to a class, not to the uploader. `document.uploaded_by` is audit metadata.
5. Students can retrieve documents only for classes in which they are actively enrolled.
6. Trainers can manage documents only for classes assigned to them.
7. Documents must be approved before indexing for retrieval.
8. Enforce these rules in FastAPI; do not rely on frontend filtering.

## Qdrant boundary

Qdrant is external vector storage, not a PostgreSQL table in this ERD. Vector records should include identifiers and metadata such as `document_id`, `class_id`, the corresponding `document_chunk.id`, `page_number`, `chapter`, and `source_type`. Filter retrieval by class to prevent cross-class data exposure. Coordinate PostgreSQL and Qdrant updates in the ingestion workflow and handle failures/re-indexing.

## Migration rule

This is the project's single source of truth for the ERD.

1. Confirm migration table names and columns match this document.
2. If the team approves a data-model change, update this document first.
3. Update migrations and backend models to match.
4. Other user stories must reference this ERD instead of creating another one.
