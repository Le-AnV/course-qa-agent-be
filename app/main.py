import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config.config import get_settings
from app.routers.admin.router import router as admin_router

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize database connection and default records."""
    try:
        from app.core.database import SessionLocal, check_db_connection
        from app.core.seed import seed_database

        if check_db_connection():
            with SessionLocal() as db:
                seed_database(db)
            logger.info("Database initialized and verified successfully.")
        else:
            logger.warning("Database unreachable during startup; using fallback mode.")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Database startup check skipped or failed: {e}")
    yield


app = FastAPI(title="Course QA Agent API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Per-Epic routers land here as sprints progress, e.g.:
from app.routers.auth.router import router as auth_router

app.include_router(auth_router)
app.include_router(admin_router)

# from app.routers import documents, chat, tickets, admin
# app.include_router(documents.router)
# app.include_router(chat.router)
# app.include_router(tickets.router)
# app.include_router(admin.router)


@app.get("/health")
def health_check():
    """Health check for Docker, the frontend and uptime monitors (AI-112 AC-02)."""
    return {"status": "ok", "service": "course-qa-agent-backend"}
