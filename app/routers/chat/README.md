# routers/chat — Epic E4: Contextual Q&A (T15–T20) — 48đ, lớn nhất

SSE streaming chat endpoint + agent orchestration. Prefix: `/api/chat`.

| Jira | Story | Assignee | Status |
|---|---|---|---|
| AI-59 | Student Chat Interface (FE part) | Nguyễn Chí Trung | To Do |
| AI-61 | RAG Q&A Backend (retrieve + generate) | Unassigned | To Do |
| AI-63 | Contextual Follow-up Questions | Unassigned | To Do |
| AI-65 | Retrieval Score Threshold (KB vs Internet) | Unassigned | To Do |
| AI-67 | Internet Source Agent | Unassigned | To Do |
| AI-69 | Chat Processing Status & System Errors | Unassigned | To Do |

**Flow (TECHSTACK §3.1):**
1. Embed query → Qdrant top-k **filtered by student's class** (student cannot pick class).
2. LangChain Agent (Gemini): score ≥ threshold (default 0.8) → answer from KB; score < threshold → `web_search` tool.
3. **Always returns a result, 100% confident** — internet sources carry a separate label.
4. Stream via SSE; keep query language (vi→vi, en→en).

**Key notes:** threshold configurable in E8 (AI-107); web_search provider TBD from mentor — stub it meanwhile (TECHSTACK §7.4). Core logic in `services/rag_service.py`.
