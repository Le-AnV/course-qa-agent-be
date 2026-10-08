# routers/documents — Epic E2: Knowledge Base (T5–T9)

Document upload / list / search / tag / delete, scoped by class. Prefix: `/api/documents`.

| Jira | Story | Assignee | Status |
|---|---|---|---|
| AI-35 | Upload Course Materials (9 formats) | nguyenvu040905 | To Do |
| AI-37 | Document List & Search | Khang Vu | In Progress |
| AI-39 | Delete & Replace Documents | Unassigned | To Do |
| AI-41 | Tag Documents by Class | nguyenvu040905 | To Do |
| AI-43 | Preview & Download Original Documents | Unassigned | To Do |

**Key AC (TECHSTACK v6/v7)**
- 9 formats: `.pdf .docx .md .txt .pptx .xlsx .csv .png .jpg` — validate real MIME (AI-35).
- Upload screen is documents-only; account import is admin's separate flow (AI-35 AC-08).
- Class must exist and be created by admin — trainer cannot create classes (AI-41 / v7).
- Trainer only sees/deletes documents of their own class (payload filter by class).

**Depends on:** `models/document.py`, E3 ingestion queue (upload → pending approval).
