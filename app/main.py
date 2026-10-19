"""Course QA Agent backend — FastAPI modular monolith entrypoint."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.core.database import create_all_tables, get_session_local, init_engine
from app.core.deps import get_db
from app.core.seed import seed_database
from app.routers.admin.router import router as admin_router
from app.routers.auth.router import router as auth_router
from app.routers.class_config import router as class_config_router
from app.routers.documents import router as documents_router
from app.routers.history import router as history_router
from app.routers.student.router import router as student_router
from app.services.document_service import fail_stale_processing
from app.services.system_health_service import build_health

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # DB tables for E1/AI-31; Postgres/Qdrant checks stay in later epics.
    try:
        init_engine(settings.database_url)
        create_all_tables()
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
        logger.warning("Database unavailable at startup; skipping table creation: %s", exc)
    yield


app = FastAPI(title="Course QA Agent API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Per-Epic routers land here as sprints progress.
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(class_config_router)
app.include_router(documents_router)
app.include_router(history_router)
app.include_router(student_router)



@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    """Component health for Docker, the frontend and uptime monitors (AI-109).

    Fast path: DB and Qdrant are probed live, the LLM is reported by
    configuration readiness only so the 30s Docker healthcheck never calls
    Gemini. The admin dashboard uses /api/admin/system/health for a live LLM
    probe. Always returns HTTP 200 — the body carries the per-component status.
    """

    return build_health(db, settings, live_llm=False)
