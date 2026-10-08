# routers/auth — Epic E1: User Management (T1–T4)

Thin HTTP layer: validate + enforce RBAC, delegate to services. Prefix: `/api/auth`, `/api/admin`.

| Jira | Story | Assignee | Status |
|---|---|---|---|
| AI-25 | Account Login (No Registration) | Hào | In Progress |
| AI-27 | Role-Based Access Control (RBAC) | Hào | In Progress |
| AI-29 | Personal Profile Management | Bui Trong Suu | In Progress |
| AI-31 | Course, Class, Account & Bulk Import Management | Khang Vu | In Progress |

**Key AC (from TECHSTACK v7)**
- No self-registration — every account created by admin (AI-31).
- Server reads role; re-check role server-side each request; role change requires re-login (AI-27).
- Login errors must not reveal whether an email exists (AI-25).
- Admin: create Course → Class (1 class = 1 trainer) → bulk import `.xlsx` with random passwords sent by email (queue + rate limit) (AI-31).
- Trainer does NOT create courses/classes.

**Planned files:** `router.py` (or `__init__.py` + endpoint modules).
