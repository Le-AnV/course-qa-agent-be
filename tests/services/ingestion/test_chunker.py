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
        assert c.metadata["locator"] in (
            "Course Information",
            "Expected Outcomes",
            "Training Schedule",
        )


def test_markdown_chunker_metadata_override_safety():
    markdown = "<!-- Slide number: 5 -->\n# Slide Title\nContent"
    chunks = markdown_chunker(markdown, metadata={"file_name": "test.pptx", "locator": "Initial"})
    assert chunks[0].metadata["locator"] == "Slide 5"
    assert chunks[0].metadata["file_name"] == "test.pptx"


def test_markdown_chunker_section_path_and_page_numbers():
    markdown = """# Chapter 1: Introduction
<!-- page break -->
## Section 1.1: Foundations
Text on page 2.
<!-- page break -->
More text on page 3.
"""
    chunks = markdown_chunker(markdown, metadata={"file_name": "book.pdf"})
    assert len(chunks) >= 2
    # Section path must be present and hierarchical
    assert any(
        "Chapter 1: Introduction > Section 1.1: Foundations" in c.metadata.get("section_path", "")
        for c in chunks
    )
    # Page numbers must be preserved
    pages = [c.metadata.get("page_number") for c in chunks]
    assert 2 in pages
    assert 3 in pages


def test_markdown_chunker_table_preserves_headers():
    table_lines = [
        "| Col1 | Col2 | Col3 |",
        "| --- | --- | --- |",
    ]
    for i in range(15):
        table_lines.append(f"| val_{i}_1 | val_{i}_2 | val_{i}_3 |")
    markdown = "# Data Table\n" + "\n".join(table_lines)

    docs = markdown_chunker(markdown)
    sub_chunks = recursive_chunker(docs, chunk_size=120, chunk_overlap=20)
    assert len(sub_chunks) > 1
    table_chunks = [c for c in sub_chunks if c.metadata.get("table_header")]
    assert table_chunks
    assert all(c.metadata["table_header"] == "\n".join(table_lines[:2]) for c in table_chunks)
    assert sum(c.page_content.count("| Col1 | Col2 | Col3 |") for c in sub_chunks) == 1


def test_markdown_chunker_does_not_leak_later_headers_onto_earlier_chunks():
    markdown = """# A
alpha body
<!-- page break -->
intro on page two
## B
beta body
"""
    chunks = markdown_chunker(markdown, metadata={"file_name": "test.pdf"})

    intro = next(c for c in chunks if c.page_content == "intro on page two")
    assert intro.metadata.get("Header 1") == "A"
    assert "Header 2" not in intro.metadata


def test_recursive_chunker_keeps_table_header_when_table_is_smaller_than_chunk():
    table = "| Col1 | Col2 |\n| --- | --- |\n" + "\n".join(
        f"| val_{i} | other_{i} |" for i in range(3)
    )
    text = f"Intro text before the table.\n{table}\nOutro text after the table."

    chunks = recursive_chunker([Document(page_content=text)], chunk_size=100, chunk_overlap=0)

    table_chunks = [c for c in chunks if "|" in c.page_content]
    assert table_chunks
    assert all(c.metadata["table_header"] == "| Col1 | Col2 |\n| --- | --- |" for c in table_chunks)


def test_recursive_chunker_does_not_label_preamble_with_next_section():
    chunks = recursive_chunker(markdown_chunker("preamble text\n\n# A\nbody of A"))

    preamble = next(c for c in chunks if "preamble text" in c.page_content)
    assert preamble.metadata.get("section_path") is None


def test_recursive_chunker_does_not_merge_different_documents():
    chunks = recursive_chunker(
        [
            Document(page_content="short a", metadata={"document_id": "A", "page_number": 1}),
            Document(page_content="short b", metadata={"document_id": "B", "page_number": 1}),
        ]
    )

    assert len(chunks) == 2
    assert {c.metadata["document_id"] for c in chunks} == {"A", "B"}


def test_recursive_chunker_ignores_tables_inside_fenced_code():
    table = "| Col1 | Col2 |\n| --- | --- |\n" + "\n".join(
        f"| val_{i} | other_{i} |" for i in range(20)
    )
    markdown = f"```markdown\n{table}\n```\n\nAfter the code block."

    chunks = recursive_chunker([Document(page_content=markdown)], chunk_size=200, chunk_overlap=0)

    assert sum(c.page_content.count("| Col1 | Col2 |") for c in chunks) == 1


def test_markdown_chunker_ignores_locator_markers_inside_fenced_code():
    markdown = """```html
<!-- page break -->
```
# Title
Body on page one.
<!-- page break -->
Body on page two.
"""
    chunks = markdown_chunker(markdown, metadata={"file_name": "test.pdf"})

    assert {c.metadata.get("page_number") for c in chunks} == {1, 2}
    assert any("```html\n<!-- page break -->\n```" in c.page_content for c in chunks)


def test_markdown_chunker_explicit_page_numbers():
    markdown = """<!-- Page number: 1 -->
<!-- Page number: 2 -->
# Page 2 Title
Content on page two.
<!-- Page number: 3 -->
# Page 3 Title
Content on page three.
"""
    chunks = markdown_chunker(markdown, metadata={"file_name": "manual.pdf"})
    assert len(chunks) >= 2
    page_numbers = [c.metadata.get("page_number") for c in chunks]
    assert 1 not in page_numbers  # empty page 1 was skipped
    assert 2 in page_numbers
    assert 3 in page_numbers
    assert any(c.metadata.get("locator") == "Page 2" for c in chunks)
    assert any(c.metadata.get("locator") == "Page 3" for c in chunks)


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

