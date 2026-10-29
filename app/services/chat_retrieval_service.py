"""Class-scoped retrieval and reranking for the AI-61 document chat."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.services import rag_service
from app.services.rag_service import RetrievalDecision, ScoredChunk
from app.services.reranker_service import (
    RERANK_CANDIDATES,
    RERANK_TOP_K,
    calculate_margin,
    count_clusters,
    rerank_chunks,
)

logger = logging.getLogger(__name__)
SOURCE_NONE = "none"
NO_DOCUMENT_EVIDENCE_MESSAGE = (
    "I could not find enough supporting information in this class's indexed "
    "documents. Please rephrase your question or ask your trainer to add the material."
)


def retrieve_for_chat(
    db: Session,
    *,
    question: str,
    class_id: uuid.UUID | str,
    search_query_en: str | None = None,
) -> RetrievalDecision:
    """Retrieve broadly, rerank, and retain only evidence from indexed class docs."""

    threshold_high = rag_service.get_retrieval_threshold(db)
    threshold_low = rag_service.get_unrelated_threshold(db)
    if not question or not question.strip():
        return RetrievalDecision(
            question=question,
            class_id=str(class_id),
            threshold=threshold_high,
            unrelated_threshold=threshold_low,
            top_score=None,
            source=SOURCE_NONE,
            chunks=(),
        )
    try:
        candidates = rag_service.retrieve_class_chunks(
            db,
            question=question,
            class_id=class_id,
            top_k=RERANK_CANDIDATES,
        )
        if search_query_en and search_query_en.strip() and search_query_en.strip().lower() != question.strip().lower():
            try:
                en_candidates = rag_service.retrieve_class_chunks(
                    db,
                    question=search_query_en.strip(),
                    class_id=class_id,
                    top_k=RERANK_CANDIDATES,
                )
                seen_keys: set[tuple[str, int | None, str]] = set()
                merged: list[ScoredChunk] = []
                for c in candidates + en_candidates:
                    key = (c.content[:100], c.page, c.document_id)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        merged.append(c)
                candidates = sorted(merged, key=lambda item: item.score, reverse=True)[:RERANK_CANDIDATES]
            except Exception:
                logger.warning("English retrieval expansion failed; proceeding with base candidates.", exc_info=True)

        if not candidates:
            return RetrievalDecision(
                question=question,
                class_id=str(class_id),
                threshold=threshold_high,
                unrelated_threshold=threshold_low,
                top_score=None,
                source=SOURCE_NONE,
                chunks=(),
            )

        top_candidate_score = candidates[0].score
        # Fail-fast optimization: if the best candidate is already below the lower
        # threshold, exit immediately as unrelated without running the heavy
        # PyTorch Cross-Encoder transformer model on CPU.
        if top_candidate_score is not None and top_candidate_score < threshold_low:
            return RetrievalDecision(
                question=question,
                class_id=str(class_id),
                threshold=threshold_high,
                unrelated_threshold=threshold_low,
                top_score=top_candidate_score,
                source=rag_service.SOURCE_UNRELATED,
                chunks=(),
            )

        ranked_candidates = rerank_chunks(question, candidates, top_k=len(candidates))
        if search_query_en and search_query_en.strip() and search_query_en.strip().lower() != question.strip().lower():
            try:
                ranked_en = rerank_chunks(search_query_en.strip(), candidates, top_k=len(candidates))
                en_scores = {c.content: c.score for c in ranked_en}
                combined = []
                for c in ranked_candidates:
                    en_score = en_scores.get(c.content, 0.0)
                    best_score = max(c.score, en_score)
                    combined.append(ScoredChunk(content=c.content, score=best_score, metadata=c.metadata))
                ranked_candidates = sorted(combined, key=lambda item: item.score, reverse=True)
            except Exception:
                logger.warning("English rerank expansion failed; proceeding with base reranked chunks.", exc_info=True)

        chunks = ranked_candidates[:RERANK_TOP_K]
        cluster_count = count_clusters(ranked_candidates, (threshold_high + threshold_low) / 2.0)
        margin = calculate_margin(ranked_candidates)
    except Exception:
        logger.warning("Class retrieval or Cross Encoder reranking failed.", exc_info=True)
        raise

    top_score = max((chunk.score for chunk in chunks), default=None)
    if not chunks:
        source = SOURCE_NONE
    elif top_score is not None and top_score >= threshold_high:
        source = rag_service.SOURCE_CURRICULUM
    elif top_score is not None and top_score >= threshold_low:
        source = rag_service.SOURCE_INTERNET
    else:
        source = rag_service.SOURCE_UNRELATED

    return RetrievalDecision(
        question=question,
        class_id=str(class_id),
        threshold=threshold_high,
        unrelated_threshold=threshold_low,
        top_score=top_score,
        source=source,
        chunks=tuple(chunks),
        cluster_count=cluster_count,
        margin=margin,
    )
