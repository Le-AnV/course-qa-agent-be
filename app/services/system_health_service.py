"""AI-109 Health Check & System Monitoring.

Probes the three runtime dependencies of the backend and rolls them into a
single green/red report:

- ``db``     — PostgreSQL (``SELECT 1`` on the request session)
- ``qdrant`` — Qdrant vector store (``get_collections`` with a short timeout)
- ``llm``    — Google Gemini API (key + model availability)

Two entry points share the same probes:

- ``build_health(..., live_llm=False)`` — fast path for the public ``/health``
  liveness endpoint (Docker/uptime). The LLM is reported by *configuration
  readiness* only, so the 30s Docker healthcheck never calls Gemini.
- ``build_health(..., live_llm=True)`` — admin monitoring path; performs a live
  LLM probe whose result is cached for a short TTL so repeated dashboard
  refreshes do not spam the provider.

Errors are logged without secrets (AC-03): provider exceptions are reduced to
a safe message by ``llm_service`` and the log lines below never include keys.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import Settings
from app.schemas.system_health import ComponentHealth, SystemHealthRead

logger = logging.getLogger(__name__)

SERVICE_NAME = "course-qa-agent-backend"

# Live LLM probe TTL — keeps dashboard refreshes from hitting Gemini every call.
LLM_CACHE_TTL_SECONDS = 60
# Short network timeout so a down dependency cannot stall a health request.
PROBE_TIMEOUT_SECONDS = 3

_llm_cache: tuple[float, ComponentHealth] | None = None
_llm_lock = threading.Lock()


def reset_llm_cache() -> None:
    """Clear the cached LLM probe (used by tests and explicit refresh)."""

    global _llm_cache
    with _llm_lock:
        _llm_cache = None


def _check_database(db: Session) -> ComponentHealth:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any failure means "down"
        logger.warning("Health check: database unreachable: %s", exc)
        return ComponentHealth(
            name="db", status="down", healthy=False, detail="PostgreSQL unreachable"
        )
    return ComponentHealth(
        name="db", status="up", healthy=True, detail="PostgreSQL reachable"
    )


def _check_qdrant(config: Settings) -> ComponentHealth:
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(
            url=config.qdrant_url,
            api_key=config.qdrant_api_key,
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        client.get_collections()
    except Exception as exc:  # noqa: BLE001 - any failure means "down"
        logger.warning("Health check: Qdrant unreachable: %s", exc)
        return ComponentHealth(
            name="qdrant", status="down", healthy=False, detail="Qdrant unreachable"
        )
    return ComponentHealth(
        name="qdrant", status="up", healthy=True, detail="Qdrant reachable"
    )


def _check_llm_config(config: Settings) -> ComponentHealth:
    """Cheap LLM readiness for the fast path — no external call."""

    if config.google_api_key is None:
        return ComponentHealth(
            name="llm",
            status="unconfigured",
            healthy=False,
            detail="Gemini API key not configured",
        )
    return ComponentHealth(
        name="llm",
        status="configured",
        healthy=True,
        detail="Gemini API key configured",
    )


def _check_llm_live(config: Settings) -> ComponentHealth:
    """Live LLM probe: key present and the configured model is reachable."""

    from app.services.llm_service import (
        LlmConfigurationError,
        LlmConnectionError,
        check_llm_connection,
    )

    try:
        result = check_llm_connection(config)
    except LlmConfigurationError:
        return ComponentHealth(
            name="llm",
            status="unconfigured",
            healthy=False,
            detail="Gemini API key not configured",
        )
    except LlmConnectionError:
        logger.warning("Health check: Gemini API unreachable")
        return ComponentHealth(
            name="llm", status="down", healthy=False, detail="Gemini API unreachable"
        )
    return ComponentHealth(
        name="llm", status="up", healthy=True, detail=result.message
    )


def _check_llm_live_cached(config: Settings) -> ComponentHealth:
    global _llm_cache
    now = time.monotonic()
    with _llm_lock:
        cached = _llm_cache
    if cached is not None and now - cached[0] < LLM_CACHE_TTL_SECONDS:
        return cached[1]
    result = _check_llm_live(config)
    with _llm_lock:
        _llm_cache = (now, result)
    return result


def build_health(db: Session, config: Settings, *, live_llm: bool = False) -> SystemHealthRead:
    """Probe every dependency and roll the results into one report."""

    components = [
        _check_database(db),
        _check_qdrant(config),
        _check_llm_live_cached(config) if live_llm else _check_llm_config(config),
    ]
    healthy = all(component.healthy for component in components)
    return SystemHealthRead(
        status="ok" if healthy else "degraded",
        service=SERVICE_NAME,
        components=components,
    )
