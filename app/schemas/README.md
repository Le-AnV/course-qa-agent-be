# backend/app/schemas — Pydantic request/response models

One file per Epic, named after the router module: `auth.py`, `documents.py`, `ingestion.py`, `chat.py`, `citations.py`, `tickets.py`, `history.py`, `admin.py`.

**Rules**
- Routers validate input/output with these — routers stay thin (TECHSTACK §8).
- Response schemas never include secrets (password hashes, API keys, SMTP app password) — AI-105 explicitly masks them.
- Naming: `<Entity>Create`, `<Entity>Update`, `<Entity>Read`, `<Entity>List`.
