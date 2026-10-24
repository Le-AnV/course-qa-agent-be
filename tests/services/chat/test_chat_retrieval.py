"""Class-scoped retrieval and evidence decisions for the AI-61 chat path."""

from __future__ import annotations

from app.services import chat_retrieval_service, rag_service


def _chunk(content: str, *, score: float, name: str) -> rag_service.ScoredChunk:
    return rag_service.ScoredChunk(
        content=content,
        score=score,
        metadata={"document_id": name, "file_name": f"{name}.pdf", "page_number": 1},
    )


def test_low_cosine_score_can_still_answer_when_reranked_evidence_is_sufficient(
    monkeypatch,
) -> None:
    """A relevant chunk below AI-65's cosine threshold should reach evidence review."""
    candidate = _chunk(
        "Water evaporates, condenses, and returns as precipitation.",
        score=0.774,
        name="water-cycle",
    )
    calls: dict[str, object] = {}

    def retrieve(db, *, question, class_id, top_k=None):
        calls.update(question=question, class_id=class_id, top_k=top_k)
        return [candidate]

    def rerank(question, chunks, *, top_k=None):
        calls["rerank_question"] = question
        return list(chunks)

    monkeypatch.setattr(rag_service, "retrieve_class_chunks", retrieve)
    monkeypatch.setattr(rag_service, "get_retrieval_threshold", lambda db: 0.75)
    monkeypatch.setattr(chat_retrieval_service, "rerank_chunks", rerank)

    decision = chat_retrieval_service.retrieve_for_chat(
        object(),
        question="What are the main stages of the water cycle?",
        class_id="class-a",
    )

    assert decision.source == rag_service.SOURCE_CURRICULUM
    assert decision.top_score == 0.774
    assert decision.chunks == (candidate,)
    assert calls["class_id"] == "class-a"
    assert calls["top_k"] > 1


def test_retrieval_returns_no_source_when_no_live_class_chunks(monkeypatch) -> None:
    monkeypatch.setattr(rag_service, "retrieve_class_chunks", lambda *a, **k: [])
    monkeypatch.setattr(rag_service, "get_retrieval_threshold", lambda db: 0.8)

    decision = chat_retrieval_service.retrieve_for_chat(
        object(), question="Explain this", class_id="class-a"
    )

    assert decision.source == chat_retrieval_service.SOURCE_NONE
    assert decision.chunks == ()
    assert decision.top_score is None


def test_fail_fast_skips_rerank_when_top_candidate_below_unrelated_threshold(
    monkeypatch,
) -> None:
    """When the best vector candidate is below unrelated_threshold, skip Cross-Encoder."""
    low_candidate = _chunk("Unrelated text", score=0.42, name="doc-1")
    rerank_called = False

    def fake_rerank(*args, **kwargs):
        nonlocal rerank_called
        rerank_called = True
        return [low_candidate]

    monkeypatch.setattr(rag_service, "retrieve_class_chunks", lambda *a, **k: [low_candidate])
    monkeypatch.setattr(rag_service, "get_retrieval_threshold", lambda db: 0.75)
    monkeypatch.setattr(rag_service, "get_unrelated_threshold", lambda db: 0.65)
    monkeypatch.setattr(chat_retrieval_service, "rerank_chunks", fake_rerank)

    decision = chat_retrieval_service.retrieve_for_chat(
        object(), question="What should I have for dinner?", class_id="class-a"
    )

    assert not rerank_called, "Cross-Encoder reranker should NOT be called for off-topic query"
    assert decision.source == rag_service.SOURCE_UNRELATED
    assert decision.chunks == ()
    assert decision.top_score == 0.42

