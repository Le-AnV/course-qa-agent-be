# AI-49 — Multi-Format Document Text Extraction & Ingestion Pipeline

## Scope and team decision

This feature implements the multi-format document text extraction, sanitization, and structured parsing capabilities for the Course QA Agent ingestion pipeline. It allows trainers to upload diverse course materials (`.docx`/`.doc`, `.pptx`/`.ppt`, `.pdf`, `.xlsx`/`.xls`, `.md`, `.txt`) and normalizes all extracted content into clean, semantic Markdown suitable for downstream header-based chunking and vector embedding.

The team established the following architectural standards:
- **Modular Loaders:** Extractors are decoupled into format-specific modules under `app/services/ingestion/loader/` (`docx_loader.py`, `powerpoint_loader.py`, `pdf_loader.py`, `excel_loader.py`, `text_loader.py`) ensuring single responsibility.
- **Legacy Office Support:** Seamlessly support legacy binary formats (`.doc`, `.ppt`) via automated headless format conversion to OpenXML standards prior to extraction.
- **Markdown Normalization:** Every loader outputs a standard dictionary contract containing `markdown` (str), `metadata` (dict), and `images` (dict[str, Image.Image]).
- **Physical Page & Slide Preservation:** Embedded markers (`<!-- page break -->` and `<!-- Slide number: X -->`) preserve page and slide boundaries through the parser to downstream chunking without polluting raw vector text.
- **Fail-Fast Empty Check:** Every loader validates content and raises `ValueError` if a file is empty or contains no readable text, satisfying AC-07 before reaching vector indexing.

---

## Architecture and Components

| Module/Component | Purpose and use |
|---|---|
| `app/services/ingestion/loader/docx_loader.py` | Extracts `.docx` via `mammoth` and `HtmlConverter`. Maps heading levels (`Heading 1..6` $\to$ `#..######`). Scans XML (`w:lastRenderedPageBreak`, `w:br`) to preserve page breaks. Converts legacy `.doc` via headless document conversion. |
| `app/services/ingestion/loader/powerpoint_loader.py` | Extracts `.pptx` and `.ppt` slide-by-slide using `python-pptx`. Extracts text frames, shapes, tables, and speaker notes. Emits `<!-- Slide number: X -->` anchors. |
| `app/services/ingestion/loader/pdf_loader.py` | Extracts `.pdf` text via `pymupdf4llm` with `page_chunks=True`. Inserts `<!-- page break -->` separators and extracts raw embedded images with PyMuPDF. |
| `app/services/ingestion/loader/excel_loader.py` | Extracts standardized FPT course syllabus `.xlsx`/`.xls` worksheets (*cover*, *syllabus*, *schedule*, *history*) using `openpyxl`. Validates non-empty content and file existence. |
| `app/services/ingestion/loader/text_loader.py` | Loads `.md` and `.txt` files with UTF-8 / `utf-8-sig` encoding detection and whitespace validation. |
| `app/services/ingestion/parser.py` | Central dispatcher (`document_loader`). Inspects file extensions, routes to the corresponding loader, and applies post-extraction cleaning. |
| `app/services/ingestion/cleaner.py` | Sanitizes extracted Markdown: strips zero-width spaces, ligatures, hyphenated line wraps, extra whitespace, while protecting code blocks, math expressions, and slide comment markers. |
| `app/services/ingestion/chunker.py` | Chunker engine supporting locators (`Page X`, `Slide X`, section headers for Excel). Drops `page_number` for Excel documents and strips internal comment tags before vector storage. |

---

## Acceptance Criteria Status

| AC | Description | Status | Implementation Details |
|---|---|:---:|---|
| **AC-01** | Reads `.docx`/`.doc` while preserving heading/paragraph structure | **Implemented** | Handled by `docx_loader.py` using `mammoth` style maps and `HtmlConverter`. Converts `.doc` via headless document conversion. Preserves page breaks. |
| **AC-02** | Reads `.md` (keeping headings) and `.txt` (UTF-8) | **Implemented** | Handled by `text_loader.py`. Preserves heading hierarchy and falls back safely across UTF-8 encodings. |
| **AC-03** | Reads `.pptx`/`.ppt` slide by slide, keeping slide number and text | **Implemented** | Handled by `powerpoint_loader.py`. Reads titles, body text, tables, speaker notes, and tags slide locators. |
| **AC-04** | Reads `.xlsx` syllabus structure across sheets | **Implemented** | Handled by `excel_loader.py`. Maps syllabus sections dynamically via column A markers and table parsers. |
| **AC-06** | Every extracted piece keeps its locator for citations | **Implemented** | Preserved via `chunker.py`: `locator` set to `"Page X"` (PDF/DOCX), `"Slide X"` (PPTX), or deepest Header (Excel/MD). |
| **AC-07** | Empty or corrupt files reported as Failed instead of empty chunks | **Implemented** | All loaders raise `ValueError` on empty/unreadable text and `FileNotFoundError` on missing files. |
| **AC-08** | Output normalized to single LangChain `Document` chunks | **Implemented** | Standardized `Document` objects with sanitized `page_content` and uniform metadata schema. |

---

## How to Verify

### 1. Run Unit Test Suite
Execute the entire ingestion test suite (45 tests covering loaders, cleaner, parser, and chunker):

```bash
uv run pytest tests/services/ingestion/ -v
```

### 2. Verify Code Style and Linter
Ensure all ingestion modules strictly adhere to Ruff linting and formatting rules:

```bash
uv run ruff check app/services/ingestion/ tests/services/ingestion/
```

---

## Out of Scope / Not Included in AI-49

- **File Storage Persistence (AI-55):** Storing uploaded file bytes into persistent blob storage (MinIO or local disk) upon trainer upload.
- **Asynchronous Ingestion Service (AI-55):** Background task worker orchestrating approval queue transitions to vector indexing.
- **Embedding Generation & Vector Upsert (AI-53):** Embedding chunk vectors via Gemini and persisting payloads into Qdrant.
- **CSV Format:** CSV handling is excluded from document ingestion per project scope.

---

## Related Integration Note

All loaders return a dictionary conforming to:
```python
{
    "markdown": str,
    "metadata": {"source": str, "type": str, ...},
    "images": dict[str, Image.Image],
}
```
When passed into `chunker.markdown_chunker()`, the output consists of LangChain `Document` objects containing cleaned text and metadata (`file_name`, `file_type`, `locator`, and optional `page_number`/`slide_number`), ready for direct ingestion by `QdrantStore.add_documents()` in AI-53.

