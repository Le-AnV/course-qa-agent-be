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

**AI-61 document-only chat flow:**
1. Embed the query and retrieve up to 20 chunks from Qdrant, filtered by the requested class; the API verifies the caller's class access.
2. Keep only indexed documents and rerank candidates with the configured multilingual Cross Encoder.
3. Send the best five chunks to one grounded Gemini answer stream. The answer prompt requires an abstention when the selected passages do not support the answer.
4. Stream retrieval/generation progress stages, answer tokens, and citations via SSE; preserve the query language.

The streaming AI-61 endpoint does not search the web. AI-65's separate source-routing service and AI-67's Internet provider remain outside this document-only chat path. The Cross Encoder model is downloaded on first use and cached in the Compose `reranker_cache` volume.
