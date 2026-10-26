"""Local Cross Encoder reranking for retrieved course material chunks."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from app.config import settings
from app.services.rag_service import ScoredChunk

RERANK_CANDIDATES = 10
RERANK_TOP_K = int(os.getenv("RERANK_TOP_K", "3"))
MAX_SEQUENCE_LENGTH = 512

_model_lock = threading.Lock()
_loaded_model: tuple[Any, Any, Any] | None = None
logger = logging.getLogger(__name__)


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


def rerank_chunks(
    question: str,
    chunks: list[ScoredChunk],
    *,
    top_k: int = RERANK_TOP_K,
) -> list[ScoredChunk]:
    """Return the most relevant chunks ranked by Cross Encoder logits."""

    if not chunks or top_k <= 0:
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
        logits = model(**encoded).logits.squeeze(-1).detach().cpu().tolist()

    ranked = sorted(zip(chunks, logits, strict=True), key=lambda item: item[1], reverse=True)
    return [chunk for chunk, _score in ranked[:top_k]]
