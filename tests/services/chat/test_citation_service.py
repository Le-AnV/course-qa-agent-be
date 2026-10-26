from types import SimpleNamespace

from app.services.citation_service import (
    build_universal_jump_url,
    extract_best_sentence,
    verify_citations,
)


def test_build_universal_jump_url_with_range():
    base_url = "https://en.wikipedia.org/wiki/C_(programming_language)"
    snippet = "C was originally developed by Dennis Ritchie between 1972 and 1973 at Bell Labs."

    url = build_universal_jump_url(base_url, snippet)
    assert "#:~:text=" in url
    assert "C%20was%20originally%20developed" in url
    assert "1973%20at%20Bell%20Labs" in url
    assert "Labs." not in url


def test_build_universal_jump_url_short_and_empty():
    assert build_universal_jump_url("https://example.com", "") == "https://example.com"
    assert build_universal_jump_url("https://example.com", None) == "https://example.com"
    assert build_universal_jump_url("https://example.com", "Dennis Ritchie") == "https://example.com#:~:text=Dennis%20Ritchie"


def test_build_universal_jump_url_with_markdown_and_hyphen():
    base_url = "https://example.com/blog/ai-sdlc"
    snippet = "Home > Blog\n\n**An AI-led SDLC** accelerates delivery and improves developer productivity across the board."
    url = build_universal_jump_url(base_url, snippet)
    assert "#:~:text=" in url
    assert "**" not in url
    # Hyphen must be percent-encoded as %2D according to W3C spec
    assert "%2D" in url
    assert "Home" not in url  # Should select the coherent paragraph/sentence


def test_extract_best_sentence_selects_most_relevant_clause():
    chunk = (
        "Welcome to the course overview. "
        "The final practical exam accounts for 60% of your total grade. "
        "Students missing more than 20% of class will not be allowed to take the exam."
    )
    assertion = "How much does the practical exam count for?"
    best = extract_best_sentence(chunk, assertion)
    assert "60% of your total grade" in best
    assert "Welcome to the course" not in best


def test_verify_citations_removes_hallucinated_and_out_of_bounds_markers():
    chunks = [
        SimpleNamespace(content="Python was created by Guido van Rossum and released in 1991."),
        SimpleNamespace(content="Calculus focuses on limits, functions, derivatives, and integrals."),
    ]

    # [1] is valid, [99] is out of bounds, [2] in second sentence has NO relation to Calculus
    answer = (
        "Python was designed by Guido [1]. "
        "Albert Einstein invented relativity in Switzerland [2]. "
        "Random unsourced statement [99]."
    )

    clean_answer, valid_indices = verify_citations(answer, chunks)
    assert "[1]" in clean_answer
    assert "[2]" not in clean_answer
    assert "[99]" not in clean_answer
    assert valid_indices == [1]


def test_cited_page_citations_capped_at_max_citations():
    from app.services.rag_service import ScoredChunk, cited_page_citations

    # Create 5 distinct page chunks
    chunks = [
        ScoredChunk(content=f"Content for section {i}", score=0.9, metadata={"document_id": f"doc-{i}", "page_number": i})
        for i in range(1, 6)
    ]
    # Answer cites all 5 chunks
    answer = "Points: [1], [2], [3], [4], [5]."

    # Default max_citations is 3
    sources = cited_page_citations(chunks, answer)
    assert len(sources) == 3
    assert [s.document_id for s in sources] == ["doc-1", "doc-2", "doc-3"]

