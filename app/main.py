"""Course QA Agent backend — FastAPI modular monolith entrypoint."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.database import get_session_local, init_engine
from app.core.deps import get_db
from app.core.logging import configure_logging
from app.core.seed import seed_database
from app.routers.admin.router import router as admin_router
from app.routers.auth.router import router as auth_router
from app.routers.chat.router import router as chat_router
from app.routers.citations.router import router as citations_router
from app.routers.class_config import router as class_config_router
from app.routers.documents import router as documents_router
from app.routers.history import router as history_router
from app.routers.student.router import router as student_router
from app.services import reranker_service
from app.services.document_service import fail_stale_processing
from app.services.system_health_service import build_health

logger = logging.getLogger(__name__)
configure_logging()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # Schema is owned by Alembic (applied by the container entrypoint).
    # Startup only seeds data — never create/alter tables here, or a new model
    # silently creates tables without adding columns and drifts from migrations.
    try:
        init_engine(settings.database_url)
        # Seed initial roles and test users
        SessionLocal = get_session_local(settings.database_url)
        db = SessionLocal()
        try:
            seed_database(db)
            fail_stale_processing(db)
        finally:
            db.close()
    except (OSError, SQLAlchemyError) as exc:
        # Never crash boot when the DB is unreachable (e.g. /health in CI).
        logger.warning("Database unavailable at startup; skipping seed: %s", exc)
    try:
        await asyncio.to_thread(reranker_service.warm_up)
    except Exception:
        logger.warning("Cross Encoder warm-up failed; will load on demand.", exc_info=True)
    yield


app = FastAPI(title="Course QA Agent API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    # Local dev: any localhost/127.0.0.1 port (Vite/Next on 3000, 3100, ...).
    allow_origin_regex=settings.cors_origin_regex
    or (
        r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"
        if settings.app_env == "local"
        else None
    ),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Per-Epic routers land here as sprints progress.
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(chat_router)
app.include_router(class_config_router)
app.include_router(documents_router)
app.include_router(history_router)
app.include_router(student_router)
app.include_router(citations_router)



@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    """Component health for Docker, the frontend and uptime monitors (AI-109).

    Fast path: DB and Qdrant are probed live, the LLM is reported by
    configuration readiness only so the 30s Docker healthcheck never calls
    Gemini. The admin dashboard uses /api/admin/system/health for a live LLM
    probe. Always returns HTTP 200 — the body carries the per-component status.
    """

    return build_health(db, settings, live_llm=False)
