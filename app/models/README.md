# backend/app/models — SQLAlchemy models (PostgreSQL)

One module per table group. Source of truth for the ERD.

| Planned file | Tables | Jira |
|---|---|---|
| `user.py` | `users` (roles: admin / trainer / student), profile | AI-25, AI-27, AI-29 |
| `course.py` | `courses`, `classes`, class↔trainer assignment, bulk-import log | AI-31 |
| `document.py` | `documents` (status: pending / processing / indexed / rejected / failed), class tag | AI-35, AI-39, AI-41, AI-55 |
| `chat.py` | `conversations`, `messages` (incl. citations JSON, `used_web_search`) | AI-59, AI-61, AI-95 |
| `ticket.py` | `tickets`, `ticket_replies` (student-initiated only — system never auto-creates) | AI-81–AI-89 |
| `config.py` | `system_config` (thresholds, LLM/email settings — secrets stay in env) | AI-105, AI-107 |

**Rules**
- Design ref: ERD ticket **AI-119 (Khang Vu)** — align before writing.
- Content gap has **no counter table** (computed at read time, TECHSTACK §3.3).
- Migrations live in `backend/alembic/` (create when first model lands).
