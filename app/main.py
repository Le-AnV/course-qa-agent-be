"""Course QA Agent backend — FastAPI modular monolith entrypoint."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings

app = FastAPI(title="Course QA Agent API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Per-Epic routers land here as sprints progress, e.g.:
# from app.routers import auth, documents, chat, tickets, admin
# app.include_router(auth.router)
# app.include_router(documents.router)
# app.include_router(chat.router)
# app.include_router(tickets.router)
# app.include_router(admin.router)


@app.get("/health")
def health_check():
    """Health check for Docker, the frontend and uptime monitors (AI-112 AC-02)."""
    return {"status": "ok", "service": "course-qa-agent-backend"}
