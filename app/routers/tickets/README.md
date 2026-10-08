# routers/tickets — Epic E6: Student-Initiated Tickets + Content Gap (T24–T29) — 29đ

| Jira | Story | Assignee | Status |
|---|---|---|---|
| AI-81 | Student Opens a Question Ticket | Unassigned | To Do |
| AI-83 | Trainer Ticket Inbox | Unassigned | To Do |
| AI-85 | Trainer Answers a Ticket | Unassigned | To Do |
| AI-87 | Student Is Notified of a Ticket Reply | Unassigned | To Do |
| AI-89 | Mark a Ticket Resolved / Reopen | Unassigned | To Do |
| AI-91 | Repeated-Question Counter (Content Gap) | Unassigned | To Do |

**Hard rules (TECHSTACK §1/§3 — mentor's decisions):**
- **The system NEVER auto-creates tickets.** Ticket only when student actively presses "Ask teacher".
- Ticket routes to the **teacher of the student's class** (class-scoped ownership).
- Content gap (AI-91): counted only, shown on trainer dashboard — **no counter table in Postgres**, no ticket. Uses Qdrant collection `content_gap_queries` + RAM TTL cache (30–60 min, key includes T35 config version).

Prefix: `/api/tickets`, `/api/content-gap`.
