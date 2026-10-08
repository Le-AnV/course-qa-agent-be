# routers/admin — Epic E8: Admin & System Config (T34–T37)

System configuration + monitoring endpoints. Prefix: `/api/admin`.

| Jira | Story | Assignee | Status |
|---|---|---|---|
| AI-105 | Configure the LLM, API Key and Email Sender | Pham Doan Dong | In Progress |
| AI-107 | Configure the Embedding Model and Thresholds | Pham Doan Dong | To Do |
| AI-109 | Health Check & System Monitoring | Unassigned | To Do |
| AI-111 | System Usage Statistics | Unassigned | To Do |

**Key AC (TECHSTACK v7):**
- AI-105: SMTP host/port, sender address, **app password masked** (never logged, never returned by API), "Send test email" button + rate limit/min. API keys also masked.
- AI-107: retrieval threshold (default 0.8), content-gap thresholds (3 queries / 2 students / 30 days), embedding model selection.
- AI-109: `GET /health` already exists (`app/main.py`) — extend, don't duplicate.
- Every member uses **their own `GEMINI_API_KEY`** env (free-tier 429 avoidance).

Admin-only routes — enforce RBAC from E1.
