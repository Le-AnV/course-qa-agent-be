"""Class-scoped retrieval and reranking for the AI-61 document chat."""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from app.services import rag_service
from app.services.rag_service import RetrievalDecision
from app.services.reranker_service import RERANK_CANDIDATES, RERANK_TOP_K, rerank_chunks

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
) -> RetrievalDecision:
    """Retrieve broadly, rerank, and retain only evidence from indexed class docs."""

    threshold_high = rag_service.get_retrieval_threshold(db)
    threshold_low = rag_service.get_unrelated_threshold(db)
    try:
        candidates = rag_service.retrieve_class_chunks(
            db,
            question=question,
            class_id=class_id,
            top_k=RERANK_CANDIDATES,
        )
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

        chunks = rerank_chunks(question, candidates, top_k=RERANK_TOP_K)
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
    )
