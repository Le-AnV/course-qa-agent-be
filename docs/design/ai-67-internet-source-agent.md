# AI-67 — Internet Source Agent

> **Ticket:** AI-67 (Epic E4: Contextual Q&A, Task T19) · **Status:** Implemented
> **Branch:** `features/Implementation_Citations`
> **Related stories:** AI-61 (RAG Q&A backend), AI-65 (Retrieval Score Threshold), AI-73 (Automatic Citations).

## Purpose

When course curriculum materials do not contain sufficient evidence to answer a student's question ($Score < T_{high}$), the Internet Source Agent executes an external web search to provide a comprehensive, grounded answer while strictly labeling all web sources to avoid mixing with curriculum citations.

## Key Acceptance Criteria

- **AC-01 (Threshold Trigger):** Activated whenever retrieval confidence falls below $T_{high}$ (and remains $\ge T_{low}$).
- **AC-02 (Reliable Execution):** Executes web search and synthesizes answers with high confidence; never creates support tickets or returns unhandled errors.
- **AC-03 (Language Preservation):** Preserves the language of the student's query (e.g. Vietnamese questions search and retrieve Vietnamese sources).
- **AC-04 (Source Isolation):** External sources are labeled distinctly as `Internet source` with corresponding external URLs, and are never mixed with curriculum document citations.
- **AC-05 (Grounded Citations Only):** Cites only sources that were genuinely retrieved and read; zero fabricated URLs.
- **AC-06 (Academic & Safety Guardrails):** Refuses harmful, illegal, or non-academic content via model system instructions.

## Architecture and Flow

```
[Student Question] 
       │
       ▼
[Score < T_high] ──► [Search Internet Provider] ──► [Web Results (URLs + Snippets)]
                                                           │
                                                           ▼
                                               [Grounded Gemini Prompt (answer_internet.yaml)]
                                                           │
                                                           ▼
                                               [SSE Stream: progress (searching -> reading -> drafting)]
                                               [SSE Stream: token events]
                                               [SSE Stream: citation events (kind="internet")]
```

## Backend Implementation

1. **Internet Search Dispatcher (`app/services/rag_service.py`):**
   - `search_internet(query: str) -> list[WebResult]`: Executes search against the configured search provider.
   - `internet_citations(web_results: list[WebResult]) -> list[Citation]`: Converts web search items into structured citations with `kind="internet"`, title, and external URL.
   - Enforces strict separation from curriculum citations (`kind="curriculum"`).

2. **Prompt Template (`app/config/prompts/answer_internet.yaml`):**
   - System prompt instructions requiring factual synthesis strictly grounded in the numbered web results `[1]`, `[2]`.
   - Mandates matching the user's input language.

3. **SSE Streaming Events (`app/services/chat_service.py`):**
   - Emits informative progress stages:
     - `stage: "searching"`, `message: "Searching the web"`
     - `stage: "reading"`, `message: "Reviewing N web sources"`
     - `stage: "drafting"`, `message: "Synthesizing answer"`
   - Emits citation events: `{ "kind": "internet", "title": "...", "url": "https://..." }`.
