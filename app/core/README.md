# backend/app/core — shared kernel

Cross-cutting code used by every Epic module. No business logic here.

| File (planned) | Purpose |
|---|---|
| `config.py` | Settings via pydantic-settings (env: `DATABASE_URL`, `QDRANT_URL`, `GEMINI_API_KEY`, thresholds) — ties to E8 (AI-105, AI-107) |
| `database.py` | SQLAlchemy 2.x async engine + session factory |
| `security.py` | JWT create/verify, passlib hashing, RBAC dependency — ties to E1 (AI-25, AI-27) |
| `deps.py` | FastAPI dependencies (`get_db`, `get_current_user`, role guards) |

**Rules**
- Owned by: whoever needs it first creates it; changes here affect all modules → mention in MR description.
- Never put Epic-specific logic in `core/` — put it in the Epic's `routers/` + `services/`.
