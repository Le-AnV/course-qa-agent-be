"""PDF highlighting for cited passages."""

from __future__ import annotations

import pymupdf

from app.services.pdf_highlight_service import get_highlight_rects, highlight_pdf


def _make_pdf(path) -> None:
    doc = pymupdf.open()
    page_one = doc.new_page()
    page_one.insert_text((72, 72), "Introduction to the course syllabus.")
    page_two = doc.new_page()
    page_two.insert_text((72, 72), "Water evaporates then condenses into clouds.")
    page_two.insert_text((72, 120), "Unrelated closing remark about grading.")
    doc.save(path)
    doc.close()


def _highlight_count(pdf_bytes: bytes, page_index: int) -> int:
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        return len(list(doc[page_index].annots() or []))
    finally:
        doc.close()


def test_highlights_only_the_cited_passage_on_the_cited_page(tmp_path) -> None:
    source = tmp_path / "lesson.pdf"
    _make_pdf(source)

    result = highlight_pdf(source, 2, ["**Water** evaporates then condenses into clouds."])

    assert _highlight_count(result, 1) == 1
    assert _highlight_count(result, 0) == 0


def test_no_highlight_when_passage_is_not_on_the_page(tmp_path) -> None:
    source = tmp_path / "lesson.pdf"
    _make_pdf(source)

    result = highlight_pdf(source, 1, ["Water evaporates then condenses into clouds."])

    assert _highlight_count(result, 0) == 0


def test_out_of_range_page_returns_the_pdf_unchanged(tmp_path) -> None:
    source = tmp_path / "lesson.pdf"
    _make_pdf(source)

    assert highlight_pdf(source, 99, ["anything here at all"]).startswith(b"%PDF")


def test_get_highlight_rects_returns_coordinates(tmp_path) -> None:
    source = tmp_path / "lesson.pdf"
    _make_pdf(source)

    rects = get_highlight_rects(source, 2, ["Water evaporates then condenses into clouds."])
    assert len(rects) > 0
    first = rects[0]
    assert "x0" in first and "y0" in first and "x1" in first and "y1" in first
    assert first["x1"] > first["x0"]
    assert first["y1"] > first["y0"]


def test_filters_isolated_three_word_random_matches(tmp_path) -> None:
    source = tmp_path / "isolated.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    # Line 1: has common 3 words "is used to"
    page.insert_text((72, 72), "This device is used to measure temperature.")
    # Line 2: completely separate topic
    page.insert_text((72, 140), "Quantum mechanics governs particle behavior at nanoscale.")
    doc.save(source)
    doc.close()

    # Passage is about quantum mechanics, and happens to end with "is used to describe"
    passage = "Quantum mechanics governs particle behavior at nanoscale and is used to describe atoms."
    rects = get_highlight_rects(source, 1, [passage])
    # A matching prefix cannot justify highlighting a quote absent from the page.
    assert rects == []
