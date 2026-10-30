from langchain_core.documents import Document

from app.services.ingestion.chunker import markdown_chunker, recursive_chunker


def test_markdown_chunker_headers_and_page_numbers():
    markdown = """# Chapter 1: Introduction
This is page one content.
<!-- page break -->
## Section 1.1: Basics
This is page two content.
<!-- page break -->
### Detail 1.1.1
This is page three content.
"""
    chunks = markdown_chunker(markdown, metadata={"file_name": "test.pdf", "class": "CS101"})

    assert len(chunks) > 0
    # Check metadata inheritance
    assert all(c.metadata.get("file_name") == "test.pdf" for c in chunks)
    assert all(c.metadata.get("class") == "CS101" for c in chunks)

    # Check page numbers
    page_numbers = [c.metadata.get("page_number") for c in chunks]
    assert 1 in page_numbers
    assert 2 in page_numbers

    # Verify that <!-- page break --> tags were removed from the chunk content
    for c in chunks:
        assert "<!-- page break -->" not in c.page_content


def test_recursive_chunker():
    docs = [
        Document(
            page_content="First sentence. Second sentence. Third sentence.",
            metadata={"page_number": 1},
        ),
    ]

    chunks = recursive_chunker(docs, chunk_size=30, chunk_overlap=10)

    assert len(chunks) >= 1
    # Check that metadata was preserved
    assert all(c.metadata.get("page_number") == 1 for c in chunks)


def test_markdown_chunker_slide_locators():
    markdown = """<!-- Slide number: 1 -->
# Slide 1 Title
Slide 1 content.
<!-- Slide number: 2 -->
# Slide 2 Title
Slide 2 content without header break.
<!-- Slide number: 3 -->
Slide 3 content.
"""
    chunks = markdown_chunker(markdown, metadata={"file_name": "presentation.pptx", "type": "pptx"})

    assert len(chunks) >= 2
    assert any(c.metadata.get("slide_number") == 1 for c in chunks)
    assert any(c.metadata.get("slide_number") == 2 for c in chunks)
    assert any(c.metadata.get("locator") == "Slide 1" for c in chunks)

    # Verify that <!-- Slide number: X --> tags are cleaned from chunk text
    for c in chunks:
        assert "<!-- Slide number:" not in c.page_content


def test_markdown_chunker_excel_drops_page_number():
    markdown = """# Training Schedule
## Course Information
Technical Group: Software Engineering
### Expected Outcomes
Outcome 1: RAG Architecture
"""
    chunks = markdown_chunker(markdown, metadata={"file_name": "syllabus.xlsx", "type": "excel"})

    assert len(chunks) > 0
    # AC requirement: Excel metadata must NOT have page_number, but uses header/locator
    for c in chunks:
        assert "page_number" not in c.metadata
        assert "locator" in c.metadata
        assert c.metadata["locator"] in ("Course Information", "Expected Outcomes", "Training Schedule")


def test_markdown_chunker_cleans_bold_headers():
    markdown = """# **Clean Title**
## *Italic Section*
Some text.
"""
    chunks = markdown_chunker(markdown)
    assert chunks[0].metadata["Header 1"] == "Clean Title"
    assert chunks[0].metadata["Header 2"] == "Italic Section"


def test_markdown_chunker_metadata_override_safety():
    markdown = "<!-- Slide number: 5 -->\n# Slide Title\nContent"
    chunks = markdown_chunker(markdown, metadata={"file_name": "test.pptx", "locator": "Initial"})
    assert chunks[0].metadata["locator"] == "Slide 5"
    assert chunks[0].metadata["file_name"] == "test.pptx"


def test_markdown_chunker_locates_page_across_leading_page_break():
    markdown = "\n\n<!-- page break -->\n\nC++ Tutorial \n\nThis is page two content.\n"
    chunks = markdown_chunker(markdown, metadata={"file_name": "cpp_tutorial.pdf"})

    target = next(c for c in chunks if "This is page two content." in c.page_content)
    assert target.metadata["page_number"] == 2


def test_markdown_chunker_locates_page_for_unstripped_deep_header():
    markdown = (
        "Page one body text.\n\n<!-- page break -->\n\n"
        "# Copyright Page\n"
        "###### \uf0e3 Copyright 2025 by Tutorials Point (I) Pvt. Ltd.  \nAll rights reserved.\n"
    )
    chunks = markdown_chunker(markdown, metadata={"file_name": "cpp_tutorial.pdf"})

    target = next(c for c in chunks if "Copyright 2025" in c.page_content)
    assert target.metadata["page_number"] == 2


def test_markdown_chunker_advances_past_duplicate_chunk_text_across_pages():
    body = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
    markdown = f"# One\n{body}\n\n<!-- page break -->\n\n# Two\n{body}\n"
    chunks = markdown_chunker(markdown, metadata={"file_name": "duplicate.pdf"})

    second = next(c for c in chunks if c.metadata.get("Header 1") == "Two")
    assert second.metadata["page_number"] == 2


