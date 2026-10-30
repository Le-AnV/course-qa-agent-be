"""Local Cross Encoder reranking for retrieved course material chunks."""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections.abc import Sequence
from typing import Any

from app.config import settings
from app.services.rag_service import ScoredChunk

RERANK_CANDIDATES = 10
RERANK_TOP_K = int(os.getenv("RERANK_TOP_K", "3"))
MAX_SEQUENCE_LENGTH = 512

_model_lock = threading.Lock()
_loaded_model: tuple[Any, Any, Any] | None = None
logger = logging.getLogger(__name__)


def _sigmoid(x: float) -> float:
    """Calibrate unbounded Cross Encoder logits into a [0, 1] probability."""
    return 1.0 / (1.0 + math.exp(-max(min(x, 20.0), -20.0)))


def _load_model() -> tuple[Any, Any, Any]:
    """Load the configured multilingual sequence classifier once per process."""

    global _loaded_model
    with _model_lock:
        if _loaded_model is not None:
            return _loaded_model

        started = time.monotonic()
        logger.info("Loading Cross Encoder model %s.", settings.reranker_model)
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(settings.reranker_model)
        model = AutoModelForSequenceClassification.from_pretrained(settings.reranker_model)
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        model.to(device)
        model.eval()
        _loaded_model = (tokenizer, model, device)
        logger.info(
            "Loaded Cross Encoder model %s in %.2fs on %s.",
            settings.reranker_model,
            time.monotonic() - started,
            device,
        )
        return _loaded_model


def warm_up() -> None:
    """Load the Cross Encoder ahead of the first request."""

    try:
        _load_model()
    except Exception:
        logger.warning("Failed to warm up the Cross Encoder model.", exc_info=True)


def score_text_pair(text_a: str, text_b: str) -> float:
    """Compute calibrated semantic relevance in [0, 1] between two texts using Cross Encoder."""

    if not text_a.strip() or not text_b.strip():
        return 0.0

    try:
        tokenizer, model, device = _load_model()
        import torch

        encoded = tokenizer(
            [(text_a, text_b)],
            padding=True,
            truncation=True,
            max_length=MAX_SEQUENCE_LENGTH,
            return_tensors="pt",
        )
        encoded = {key: value.to(device) for key, value in encoded.items()}
        with torch.inference_mode():
            raw_logit = model(**encoded).logits.squeeze(-1).detach().cpu().tolist()
            logit = raw_logit[0] if isinstance(raw_logit, list) else float(raw_logit)
        return _sigmoid(logit)
    except Exception:
        logger.warning("Cross Encoder pairwise scoring failed; returning fallback 0.5", exc_info=True)
        return 0.5


def rerank_all(
    question: str,
    chunks: list[ScoredChunk],
) -> list[ScoredChunk]:
    """Return all candidate chunks ranked and calibrated by Cross Encoder."""

    if not chunks:
        return []

    tokenizer, model, device = _load_model()
    import torch

    encoded = tokenizer(
        [(question, chunk.content) for chunk in chunks],
        padding=True,
        truncation=True,
        max_length=MAX_SEQUENCE_LENGTH,
        return_tensors="pt",
    )
    encoded = {key: value.to(device) for key, value in encoded.items()}
    with torch.inference_mode():
        raw_logits = model(**encoded).logits.squeeze(-1).detach().cpu().tolist()

    logits: list[float]
    if isinstance(raw_logits, list):
        logits = [float(l) for l in raw_logits]
    else:
        logits = [float(raw_logits)]

    ranked = sorted(zip(chunks, logits, strict=True), key=lambda item: item[1], reverse=True)
    return [
        ScoredChunk(
            content=chunk.content,
            score=_sigmoid(logit),
            metadata=chunk.metadata,
        )
        for chunk, logit in ranked
    ]


def rerank_chunks(
    question: str,
    chunks: list[ScoredChunk],
    *,
    top_k: int = RERANK_TOP_K,
) -> list[ScoredChunk]:
    """Return the most relevant chunks ranked and calibrated by Cross Encoder."""

    if not chunks or top_k <= 0:
        return []
    return rerank_all(question, chunks)[:top_k]


def count_clusters(chunks: Sequence[ScoredChunk], threshold: float) -> int:
    """Count how many candidate chunks meet or exceed the cluster threshold."""

    return sum(1 for chunk in chunks if chunk.score is not None and chunk.score >= threshold)


def calculate_margin(chunks: Sequence[ScoredChunk]) -> float:
    """Calculate the score difference between top 1 and top 2 chunk."""

    if not chunks or chunks[0].score is None:
        return 0.0
    if len(chunks) == 1 or chunks[1].score is None:
        return float(chunks[0].score)
    return max(0.0, float(chunks[0].score) - float(chunks[1].score))

