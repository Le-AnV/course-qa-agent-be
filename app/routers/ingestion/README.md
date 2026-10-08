# routers/ingestion — Epic E3: Ingestion Pipeline (T10–T14)

Approval queue + processing status endpoints. The heavy pipeline lives in `services/ingestion_service.py`. Prefix: `/api/ingestion`.

| Jira | Story | Assignee | Status |
|---|---|---|---|
| AI-47 | Extract Text from PDF + describe images with Gemini Vision | Unassigned | To Do |
| AI-49 | Extract Text from Word/MD/TXT/PPTX/XLSX/CSV | Unassigned | To Do |
| AI-51 | Document Chunking | Le Van An | In Progress |
| AI-53 | Generate Embeddings & Store in Qdrant | Le Van An | To Do |
| AI-55 | Processing Status, Approval & Retry | Unassigned | To Do |

**Pipeline (TECHSTACK §3.2):**
```
upload → Pending approval → [trainer approves] → Processing
  → extract (keep locator: page/slide/sheet+row) + Gemini Vision for images
  → chunk ~500–1000 tokens → embed (text-embedding-004, 768d)
  → Qdrant upsert (payload: file/class/chapter/page) → Done | Failed (Retry, idempotent)
```

**Key AC:** never approved → `Rejected`, never indexed (AI-55). PDF scan (pure image PDF) unsupported — no OCR.
