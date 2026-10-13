# API Design & Implementation Specs — Save & Display Chat History (AI-95)

> **Ticket:** AI-95  
> **Epic:** E7 — Chat History (T30–T33)  
> **Assignee:** Bui Trong Suu  
> **Status:** Planned / In Progress  
> **Repo BE:** `course-qa-agent-be` | **Repo FE:** `course-qa-agent-fe`  
> **Target Branch:** `features/Design_SaveChatHistory`  

---

## 1. Overview & Objectives

This document establishes the Technical Implementation Specification and API Contract for **Save & Display Chat History (AI-95)**. It serves as the architectural reference and contract-first handover document between backend and frontend teams, adhering to the project's Modular Monolith architecture.

### 1.1. Business Objectives
1. Automatically initialize chat sessions (`chat_session`) and persist each Q&A turn (`chat_message`) into PostgreSQL.
2. Provide endpoints for the frontend to list saved chat sessions scoped to the authenticated user and class, sorted newest-first.
3. Allow the frontend to load the full message transcript of any selected chat session in chronological order when selected from the sidebar.
4. Enforce strict row-level security and RBAC: Users can only view and manage chat history sessions they own within their authorized classes.

### 1.2. Acceptance Criteria Mapping (AC-01 to AC-07)

| Criterion | Acceptance Requirement | Technical Layer & Solution |
|---|---|---|
| **AC-01** | Automatically save question & answer into DB after each chat turn completes | Backend service (`history_service.save_turn`) persists both User and Assistant messages after LLM generation completes |
| **AC-02** | Message contains: `id`, `session_id`, `role`, `content`, `timestamp`, `used_web_search` | `chat_message` database table and Pydantic schema `ChatMessageResponse` model these exact attributes |
| **AC-03** | First question creates a new session with `session_id`; title extracted from query summary | Backend creates `ChatSession` if `session_id` is omitted; truncates first prompt (up to 50 chars) as `title` |
| **AC-04** | List sessions by user & class, newest first | `GET /api/history?class_id={class_id}` filtered by `user_id` and optional `class_id`, ordered by `updated_at DESC` |
| **AC-05** | Load all messages of a session in chronological order | `GET /api/history/{session_id}` ordered by `created_at ASC` |
| **AC-06** | Frontend sidebar displays list of sessions, newest on top | Sidebar component fetches `GET /api/history` and renders sorted sessions |
| **AC-07** | Selecting a session loads and displays the full Q&A transcript | Sidebar selection triggers `GET /api/history/{session_id}`, populating `ChatWindow` state |

---

## 2. Database Schema Reference

Based on the canonical database schema defined in `db/core_schema.sql` (lines 134–167):

```sql
-- 1. Conversation session table
CREATE TABLE chat_session (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
    class_id UUID NOT NULL REFERENCES class(id) ON DELETE RESTRICT,
    title VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. Message table within a session
CREATE TABLE chat_message (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    role VARCHAR(20) NOT NULL,
    content TEXT NOT NULL,
    used_web_search BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_chat_message_role CHECK (
        role IN ('USER', 'ASSISTANT', 'SYSTEM')
    )
);

-- Optimization indexes
CREATE INDEX idx_chat_session_user_id ON chat_session(user_id);
CREATE INDEX idx_chat_session_class_id ON chat_session(class_id);
CREATE INDEX idx_chat_message_session_id ON chat_message(session_id);
```

---

## 3. API Contract Specifications

* **Base URL:** `/api/history`
* **Authentication:** All endpoints require an HTTP header: `Authorization: Bearer <JWT_ACCESS_TOKEN>`.
* **Standard Error Envelope:**
  ```json
  { "detail": "Human-readable error description" }
  ```

---

### 3.1. `GET /api/history` — List User Chat Sessions (AC-04)

Retrieves all chat sessions belonging to the authenticated user, optionally filtered by `class_id`. Results are sorted by `updated_at DESC`.

* **Method:** `GET`
* **URL:** `/api/history`
* **Headers:**
  * `Authorization: Bearer <token>`
* **Query Parameters:**
  * `class_id` *(UUID, optional)*: Filter sessions by class.
  * `limit` *(integer, optional, default: 50, maximum: 100)*: Maximum items to return.
  * `offset` *(integer, optional, default: 0)*: Number of items to skip.

#### Success Response (`200 OK`):
```json
{
  "items": [
    {
      "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "class_id": "7ca65e12-4211-4783-9999-1a2b3c4d5e6f",
      "title": "Explain Dijkstra algorithm in detail",
      "created_at": "2026-10-13T08:30:00Z",
      "updated_at": "2026-10-13T08:35:10Z"
    },
    {
      "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
      "class_id": "7ca65e12-4211-4783-9999-1a2b3c4d5e6f",
      "title": "Question about Week 3 homework",
      "created_at": "2026-10-12T14:20:00Z",
      "updated_at": "2026-10-12T14:25:00Z"
    }
  ],
  "total": 2
}
```

#### Error Responses:
* `401 Unauthorized`: Token is missing or invalid.
  ```json
  { "detail": "Could not validate credentials." }
  ```

---

### 3.2. `GET /api/history/{session_id}` — Get Session Messages Transcript (AC-05)

Retrieves session metadata and the full transcript of messages in chronological order (`created_at ASC`).

* **Method:** `GET`
* **URL:** `/api/history/{session_id}`
* **Headers:**
  * `Authorization: Bearer <token>`
* **Path Parameters:**
  * `session_id` *(UUID, required)*: Target session identifier.

#### Success Response (`200 OK`):
```json
{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "class_id": "7ca65e12-4211-4783-9999-1a2b3c4d5e6f",
  "title": "Explain Dijkstra algorithm in detail",
  "created_at": "2026-10-13T08:30:00Z",
  "updated_at": "2026-10-13T08:35:10Z",
  "messages": [
    {
      "id": "d0e1f2a3-b4c5-6789-0123-456789abcdef",
      "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "role": "USER",
      "content": "Can you explain Dijkstra's algorithm?",
      "used_web_search": false,
      "created_at": "2026-10-13T08:30:00Z"
    },
    {
      "id": "e1f2a3b4-c5d6-7890-1234-567890abcdef",
      "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "role": "ASSISTANT",
      "content": "Dijkstra's algorithm is an algorithm for finding the shortest paths between nodes in a graph with non-negative edge weights...",
      "used_web_search": false,
      "created_at": "2026-10-13T08:30:05Z"
    }
  ]
}
```

#### Error Responses:
* `401 Unauthorized`: Token invalid or expired.
* `403 Forbidden`: User does not own this chat session (row-level security).
  ```json
  { "detail": "Access forbidden: you do not own this chat session." }
  ```
* `404 Not Found`: Session does not exist.
  ```json
  { "detail": "Chat session not found." }
  ```

---

### 3.3. `POST /api/history/sessions` — Create New Session (AC-03)

Creates a new chat session under an authorized class.

* **Method:** `POST`
* **URL:** `/api/history/sessions`
* **Headers:**
  * `Authorization: Bearer <token>`
  * `Content-Type: application/json`
* **Request Body:**
```json
{
  "class_id": "7ca65e12-4211-4783-9999-1a2b3c4d5e6f",
  "title": "New Discussion"
}
```

#### Success Response (`201 Created`):
```json
{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "class_id": "7ca65e12-4211-4783-9999-1a2b3c4d5e6f",
  "title": "New Discussion",
  "created_at": "2026-10-13T08:30:00Z",
  "updated_at": "2026-10-13T08:30:00Z"
}
```

---

### 3.4. `POST /api/history/sessions/{session_id}/messages` — Append Message to Session (AC-01, AC-02)

Appends a new message to an existing chat session.

* **Method:** `POST`
* **URL:** `/api/history/sessions/{session_id}/messages`
* **Headers:**
  * `Authorization: Bearer <token>`
  * `Content-Type: application/json`
* **Path Parameters:**
  * `session_id` *(UUID, required)*
* **Request Body:**
```json
{
  "role": "USER",
  "content": "What is the time complexity with a min-heap?",
  "used_web_search": false
}
```

#### Success Response (`201 Created`):
```json
{
  "id": "f2a3b4c5-d6e7-8901-2345-678901abcdef",
  "session_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "role": "USER",
  "content": "What is the time complexity with a min-heap?",
  "used_web_search": false,
  "created_at": "2026-10-13T08:32:00Z"
}
```

---

### 3.5. `DELETE /api/history/{session_id}` — Delete Chat Session (AI-99 & Cleanup)

Deletes a chat session and cascades deletion to all contained messages.

* **Method:** `DELETE`
* **URL:** `/api/history/{session_id}`
* **Headers:**
  * `Authorization: Bearer <token>`

#### Success Response (`200 OK`):
```json
{
  "message": "Chat session deleted successfully."
}
```

---

## 4. Frontend TypeScript Contracts (`course-qa-agent-fe`)

Frontend type definitions that strictly align with the Backend Pydantic schemas:

```typescript
// Role enum matching backend constraint
export type ChatRole = 'USER' | 'ASSISTANT' | 'SYSTEM';

// Summary item displayed in Sidebar (AC-06)
export interface ChatSessionSummary {
  id: string;
  class_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface ChatSessionListResponse {
  items: ChatSessionSummary[];
  total: number;
}

// Single chat message model (AC-02)
export interface ChatMessageItem {
  id: string;
  session_id: string;
  role: ChatRole;
  content: string;
  used_web_search: boolean;
  created_at: string;
}

// Full session detail with ordered messages (AC-05, AC-07)
export interface ChatSessionDetailResponse {
  id: string;
  class_id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  messages: ChatMessageItem[];
}

// Request payload for session creation (AC-03)
export interface CreateChatSessionRequest {
  class_id: string;
  title?: string;
}

// Request payload for appending a message (AC-01)
export interface CreateChatMessageRequest {
  role: ChatRole;
  content: string;
  used_web_search?: boolean;
}
```

---

## 5. Backend Implementation Architecture

### 5.1. Models (`app/models/chat.py`)
* `ChatSession(Base)`:
  * Maps to table `chat_session`.
  * Fields: `id`, `user_id`, `class_id`, `title`, `created_at`, `updated_at`.
  * Relationship: `messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.created_at")`.
* `ChatMessage(Base)`:
  * Maps to table `chat_message`.
  * Fields: `id`, `session_id`, `role`, `content`, `used_web_search`, `created_at`.
  * Relationship: `session = relationship("ChatSession", back_populates="messages")`.

### 5.2. Schemas (`app/schemas/history.py`)
* `SessionCreate`: `class_id: UUID`, `title: Optional[str] = None`.
* `MessageCreate`: `role: str`, `content: str`, `used_web_search: bool = False`.
* `MessageResponse`: `id: UUID`, `session_id: UUID`, `role: str`, `content: str`, `used_web_search: bool`, `created_at: datetime`.
* `SessionSummaryResponse`: `id: UUID`, `class_id: UUID`, `title: Optional[str]`, `created_at: datetime`, `updated_at: datetime`.
* `SessionDetailResponse`: Extends `SessionSummaryResponse` with `messages: list[MessageResponse]`.
* `SessionListResponse`: `items: list[SessionSummaryResponse]`, `total: int`.

### 5.3. Service Layer (`app/services/history_service.py`)
* `list_user_sessions(db: Session, user_id: UUID, class_id: Optional[UUID] = None, limit: int = 50, offset: int = 0)`
* `get_session_by_id(db: Session, session_id: UUID, user_id: UUID)`
* `create_session(db: Session, user_id: UUID, class_id: UUID, title: Optional[str] = None)`
* `append_message(db: Session, session_id: UUID, role: str, content: str, used_web_search: bool = False)`
* `save_turn(db: Session, user_id: UUID, class_id: UUID, user_prompt: str, assistant_response: str, session_id: Optional[UUID] = None, used_web_search: bool = False)`
* `delete_session(db: Session, session_id: UUID, user_id: UUID)`

### 5.4. Router (`app/routers/history/router.py`)
* APIRouter with `prefix="/api/history"`, `tags=["history"]`.
* Injected dependencies: `current_user = Depends(get_current_user)`, `db: Session = Depends(get_db)`.

### 5.5. Application Entrypoint (`app/main.py`)
* Register the history router:
  ```python
  from app.routers.history.router import router as history_router
  app.include_router(history_router)
  ```

---

## 6. Verification & Test Plan

### 6.1. Interactive Swagger Verification
* Start BE service: `uv run uvicorn app.main:app --reload`
* Navigate to `http://localhost:8000/docs`.
* Verify all 5 endpoints appear under the `history` tag:
  * `GET /api/history`
  * `GET /api/history/{session_id}`
  * `POST /api/history/sessions`
  * `POST /api/history/sessions/{session_id}/messages`
  * `DELETE /api/history/{session_id}`

### 6.2. Automated Pytest Suite
* Target file: `tests/test_history.py`.
* Execution command:
  ```powershell
  uv run pytest tests/test_history.py -v
  ```
* Test Cases:
  * [x] **TC-01:** Create session returns HTTP 201 with UUID.
  * [x] **TC-02:** Add messages returns HTTP 201 with timestamps.
  * [x] **TC-03:** List sessions returns only caller's sessions, ordered by `updated_at DESC`.
  * [x] **TC-04:** Get session detail returns messages ordered chronologically (`created_at ASC`).
  * [x] **TC-05:** Accessing another user's session yields HTTP 403 Forbidden.
  * [x] **TC-06:** Querying non-existent session yields HTTP 404 Not Found.
  * [x] **TC-07:** Deleting session succeeds with HTTP 200 and cascades messages removal.

---

## 7. Approval & Handover

* **Author:** Bui Trong Suu
* **Reviewers:** Tech Lead, Backend & Frontend Team (AI-95)
* **Policy:** Any changes to payload contracts or HTTP status codes require updating this specification prior to code modifications.
