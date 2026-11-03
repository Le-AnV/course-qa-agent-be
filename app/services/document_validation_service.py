"""Document relevance validation before ingestion (Course Relevance Gate)."""

from __future__ import annotations

import json
import logging
import re
import uuid

from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config.config import settings
from app.core.prompt_manager import get_prompt_manager
from app.services import gemini_key_service

logger = logging.getLogger(__name__)

VALIDATION_EXCERPT_CHARS = 3500


class DocumentRelevanceError(ValueError):
    """Raised when an uploaded document does not match the course."""


class DocumentRelevanceResult(BaseModel):
    is_relevant: bool = True
    confidence: float = 1.0
    reason: str = ""


def _parse_validation_json(content: str) -> DocumentRelevanceResult:
    cleaned = (content or "").strip()
    if "```" in cleaned:
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
        if match:
            cleaned = match.group(1).strip()
        else:
            start = cleaned.find("{")
            end = cleaned.rfind("}")
            if start != -1 and end != -1:
                cleaned = cleaned[start : end + 1].strip()
    elif not cleaned.startswith("{") and "{" in cleaned:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1:
            cleaned = cleaned[start : end + 1].strip()

    try:
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError, TypeError):
        return DocumentRelevanceResult(is_relevant=True, confidence=0.0, reason="Failed to parse JSON")

    return DocumentRelevanceResult(
        is_relevant=bool(data.get("is_relevant", True)),
        confidence=float(data.get("confidence", 1.0)),
        reason=str(data.get("reason", "")),
    )


def validate_document_relevance(
    db: Session,
    *,
    class_name: str,
    class_code: str,
    class_description: str | None = None,
    file_name: str,
    document_text: str,
) -> tuple[bool, str]:
    """Determine whether an uploaded document is relevant to the course.

    Returns:
        (is_relevant, reason):
            is_relevant=True if relevant or when validation fails open.
            is_relevant=False if confidently irrelevant.
    """
    excerpt = (document_text or "")[:VALIDATION_EXCERPT_CHARS].strip()
    if not excerpt:
        return True, "Empty document excerpt"

    try:
        prompt_tmpl = get_prompt_manager().format(
            "document-relevance-prompt",
            class_code=class_code or "N/A",
            class_name=class_name or "N/A",
            class_description=class_description or "N/A",
            file_name=file_name or "Document",
            excerpt=excerpt,
        )
        system_prompt = str(prompt_tmpl["system-prompt"])
        user_prompt = str(prompt_tmpl["user-prompt"])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not format document relevance prompt: %s; failing open.", exc)
        return True, "Prompt formatting error"

    excluded_ids: set[uuid.UUID] = set()
    last_error: Exception | None = None

    for _ in range(3):
        resolved = gemini_key_service.resolve_key(
            db, model=settings.decision_model, exclude_ids=excluded_ids
        )
        if resolved is None:
            secret = settings.google_api_key
            api_key = (
                secret.get_secret_value()
                if hasattr(secret, "get_secret_value")
                else str(secret)
                if secret
                else None
            )
        else:
            api_key = resolved.api_key

        if not api_key:
            break

        try:
            client = genai.Client(api_key=api_key)
            config = genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.0,
                response_mime_type="application/json",
            )
            response = client.models.generate_content(
                model=settings.decision_model,
                contents=user_prompt,
                config=config,
            )
            text = response.text or ""
            result = _parse_validation_json(text)

            if resolved and resolved.key:
                gemini_key_service.touch_key(db, resolved.key)

            # Block only if confidently irrelevant (confidence >= 0.8)
            if not result.is_relevant and result.confidence >= 0.8:
                logger.warning(
                    "Document '%s' rejected for class '%s' (%s): %s (confidence: %.2f)",
                    file_name,
                    class_name,
                    class_code,
                    result.reason,
                    result.confidence,
                )
                return False, result.reason or "Document content is not relevant to this course."

            return True, result.reason
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if resolved and resolved.key:
                excluded_ids.add(resolved.key.id)
                gemini_key_service.mark_key_failure(db, resolved.key, exc)
            logger.warning("Document relevance attempt failed (%s); trying next key.", exc)
            if resolved is None or not resolved.key_id:
                break

    if last_error:
        logger.warning("Document relevance check failed (%s); failing open.", last_error)
        return True, f"Validation skipped due to error: {last_error}"

    logger.warning("No Gemini API key available for document relevance check; failing open.")
    return True, "No API key available"
