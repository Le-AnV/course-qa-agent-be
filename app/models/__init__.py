"""SQLAlchemy models — source of truth for the ERD (AI-119 aligns here)."""

from app.models.course import ClassSection, Course  # noqa: F401
from app.models.email_outbox import EmailOutbox  # noqa: F401
from app.models.import_job import ImportBatch, ImportRow  # noqa: F401
from app.models.system_config import SystemConfig  # noqa: F401
from app.models.user import User  # noqa: F401
