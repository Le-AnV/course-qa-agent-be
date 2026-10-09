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
        Document(page_content="First sentence. Second sentence. Third sentence.", metadata={"page_number": 1}),
    ]

    chunks = recursive_chunker(docs, chunk_size=30, chunk_overlap=10)

    assert len(chunks) >= 1
    # Check that metadata was preserved
    assert all(c.metadata.get("page_number") == 1 for c in chunks)

