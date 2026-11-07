"""Course QA Agent backend — FastAPI modular monolith entrypoint."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

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
from app.routers.categories import router as categories_router
from app.routers.chat.router import router as chat_router
from app.routers.citations.router import router as citations_router
from app.routers.class_config import router as class_config_router
from app.routers.content_gaps import router as content_gaps_router
from app.routers.documents import router as documents_router
from app.routers.history import router as history_router
from app.routers.quizzes import router as quizzes_router
from app.routers.quizzes.results import router as quiz_results_router
from app.routers.quizzes.student_results import router as student_quiz_results_router
from app.routers.student.router import router as student_router
from app.routers.tickets import router as tickets_router
from app.services import reranker_service
from app.services.document_service import fail_stale_processing
from app.services.holiday_service import seed_holidays
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
            try:
                seed_holidays(db)
            except Exception as exc:  # noqa: BLE001 - holiday seed must not block boot
                logger.warning("Holiday seed failed; will retry on next boot: %s", exc)
        finally:
            db.close()
    except (OSError, SQLAlchemyError) as exc:
        # Never crash boot when the DB is unreachable (e.g. /health in CI).
        logger.warning("Database unavailable at startup; skipping seed: %s", exc)
    try:
        await asyncio.to_thread(reranker_service.warm_up)
    except Exception:
        logger.warning("Cross Encoder warm-up failed; will load on demand.", exc_info=True)
    expiration_task = asyncio.create_task(_expire_tickets_periodically())
    try:
        yield
    finally:
        expiration_task.cancel()
        with suppress(asyncio.CancelledError):
            await expiration_task


def _expire_tickets_once() -> int:
    from app.services.ticket_service import expire_open_tickets

    session_local = get_session_local(settings.database_url)
    with session_local() as db:
        return expire_open_tickets(db)


async def _expire_tickets_periodically() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            expired = await asyncio.to_thread(_expire_tickets_once)
            if expired:
                logger.info("Expired %s unanswered tickets after seven days.", expired)
        except Exception:
            logger.warning("Ticket expiration pass failed; will retry.", exc_info=True)


_expose_api_docs = settings.app_env != "production"

app = FastAPI(
    title="Course QA Agent API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _expose_api_docs else None,
    redoc_url="/redoc" if _expose_api_docs else None,
    openapi_url="/openapi.json" if _expose_api_docs else None,
)

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
app.include_router(tickets_router)
app.include_router(quizzes_router)
app.include_router(quiz_results_router)
app.include_router(student_quiz_results_router)
app.include_router(categories_router)
app.include_router(content_gaps_router)



@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    """Component health for Docker, the frontend and uptime monitors (AI-109).

    Fast path: DB and Qdrant are probed live, the LLM is reported by
    configuration readiness only so the 30s Docker healthcheck never calls
    Gemini. The admin dashboard uses /api/admin/system/health for a live LLM
    probe. Always returns HTTP 200 — the body carries the per-component status.
    """

    return build_health(db, settings, live_llm=False)
