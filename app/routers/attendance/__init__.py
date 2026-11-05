"""Class schedule and attendance router package (AI-136)."""

from app.routers.attendance.router import router
from app.routers.attendance.student_router import student_router

__all__ = ["router", "student_router"]
