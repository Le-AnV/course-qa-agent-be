"""Course QA Agent backend — FastAPI modular monolith entrypoint."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.core.database import create_all_tables, init_engine
from app.routers.admin.router import router as admin_router
from app.routers.auth.router import mock_router as mock_auth_router
from app.routers.auth.router import router as auth_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    # DB tables for E1/AI-31; Postgres/Qdrant checks stay in later epics.
    try:
        init_engine(settings.database_url)
        create_all_tables()
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

# Per-Epic routers land here as sprints progress. The legacy mock login
# (/auth) stays until the FE login modal moves to the DB contract (/api/auth).
app.include_router(auth_router)
app.include_router(mock_auth_router)
app.include_router(admin_router)


@app.get("/health")
def health_check():
    """Health check for Docker, the frontend and uptime monitors (AI-112 AC-02)."""
    return {"status": "ok", "service": "course-qa-agent-backend"}
