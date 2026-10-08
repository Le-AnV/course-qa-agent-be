# routers/citations — Epic E5: Citations (T21–T23)

Endpoints serving citation metadata + source excerpts. Prefix: `/api/citations`.

| Jira | Story | Assignee | Status |
|---|---|---|---|
| AI-73 | Automatic Citations in Answers | Unassigned | To Do |
| AI-75 | Click a Citation to View the Source Excerpt | Unassigned | To Do |
| AI-77 | Citations Point to the Correct Chapter/Page | Unassigned | To Do |

**Key AC (TECHSTACK §6 E5):**
- Metadata chunking carries class + text/image origin (set in E3, read here).
- Citation payload: file, class, chapter, page/slide/sheet-row — must match original locator.
- **Internet sources are labeled separately** from curriculum sources — never mix.
- Excerpt endpoint returns the chunk text around the cited locator.
