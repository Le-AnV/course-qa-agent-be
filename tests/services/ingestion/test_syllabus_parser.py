"""Unit tests for syllabus markdown parser and XSS sanitation (AI-133)."""

from pathlib import Path

import pytest

from app.services.ingestion.loader.excel_loader import excel_loader
from app.services.ingestion.syllabus_parser import (
    SyllabusFormatError,
    is_syllabus_document,
    parse_syllabus_markdown,
    sanitize_markdown_content,
)

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "syllabus_roadmap_sample.xlsx"


def test_is_syllabus_document_detection():
    assert is_syllabus_document("Syllabus_Python.xlsx", "") is True
    assert is_syllabus_document("course_syllabus.md", "") is True
    assert is_syllabus_document("lecture_01.pdf", "Some random notes") is False
    assert (
        is_syllabus_document(
            "generic.txt",
            "# Training Schedule\n\n## Unit 1\nContent",
        )
        is True
    )
    assert (
        is_syllabus_document(
            "overview.md",
            "## Topic Outline\n\nUnit 01: Basics",
        )
        is True
    )


def test_sanitize_markdown_content_strips_html_and_preserves_markdown():
    dirty = """
# Unit 1
<script>alert('xss')</script>
<iframe src="http://evil.com"></iframe>
Click [here](javascript:alert(1)) or [legit](https://example.com).
<div onclick="doBad()">Click me</div>
**Bold text** and *italic text*.
- Item 1
- Item 2
"""
    clean = sanitize_markdown_content(dirty)

    assert "<script>" not in clean
    assert "alert('xss')" not in clean
    assert "<iframe>" not in clean
    assert "javascript:" not in clean
    assert "onclick" not in clean
    assert "<div" not in clean
    assert "**Bold text**" in clean
    assert "*italic text*" in clean
    assert "- Item 1" in clean
    assert "[legit](https://example.com)" in clean


def test_parse_syllabus_markdown_from_sample_xlsx():
    assert FIXTURE.is_file()

    result = excel_loader(FIXTURE)
    markdown = result["markdown"]

    roadmap = parse_syllabus_markdown(markdown)

    assert roadmap.topic_outline is not None
    assert "Unit 01: Introduction to AI & Generative AI & LangChain" in roadmap.topic_outline
    assert "Unit 02: RAG Foundations" in roadmap.topic_outline

    assert len(roadmap.units) >= 2
    # Verify sequential unit numbering starting from 1
    for index, unit in enumerate(roadmap.units, start=1):
        assert unit.unit_number == index

    u1 = roadmap.units[0]
    assert "Introduction to AI" in u1.unit_title
    assert "Session 1" in u1.content
    assert "RAG Theoretical Foundations" in u1.content
    assert "*Notes:*" not in u1.content

    u2 = roadmap.units[1]
    assert "RAG Foundations" in u2.unit_title
    assert "Session 3" in u2.content


def test_parse_syllabus_markdown_from_rag_optimization_xlsx():
    assert FIXTURE.is_file()

    result = excel_loader(FIXTURE)
    roadmap = parse_syllabus_markdown(result["markdown"])

    assert roadmap.topic_outline is not None
    assert "Tokenization" in roadmap.topic_outline
    assert len(roadmap.units) >= 5

    titles = [u.unit_title for u in roadmap.units]
    assert "Advanced Indexing" in titles
    assert "Hybrid Search" in titles
    assert "Query Transformation" in titles
    assert "Post-Retrieval Processing" in titles
    assert "GraphRAG Implementation" in titles


def test_parse_syllabus_markdown_from_direct_markdown():
    md = """# Python Course Syllabus

## Topic Outline
Unit 01: Basics
Unit 02: Advanced

## Unit 1: Python Basics
### Session 1
- **Introduction** (90 min)
  - Syntax, variables, functions

## Unit 2: Advanced OOP
### Session 2
- **Classes and Metaclasses** (120 min)
  - Inheritance and polymorphism
"""
    roadmap = parse_syllabus_markdown(md)

    assert roadmap.topic_outline is not None
    assert "Unit 01: Basics" in roadmap.topic_outline
    assert len(roadmap.units) == 2
    assert roadmap.units[0].unit_number == 1
    assert roadmap.units[0].unit_title == "Python Basics"
    assert "Session 1" in roadmap.units[0].content

    assert roadmap.units[1].unit_number == 2
    assert roadmap.units[1].unit_title == "Advanced OOP"
    assert "Classes and Metaclasses" in roadmap.units[1].content


def test_parse_syllabus_markdown_raises_format_error_on_empty_or_unrecognized():
    with pytest.raises(SyllabusFormatError, match="Syllabus format is not recognized"):
        parse_syllabus_markdown("")

    with pytest.raises(SyllabusFormatError, match="Syllabus format is not recognized"):
        parse_syllabus_markdown("Some generic lecture text without any units or schedule.")

