"""SQLAlchemy models - source of truth for the ERD (AI-119 aligns here)."""

from app.models.chat import ChatMessage, ChatSession  # noqa: F401
from app.models.course import Class, ClassConfig, ClassTrainer, Course, Enrollment  # noqa: F401
from app.models.document import Document, SyllabusUnit  # noqa: F401
from app.models.email_outbox import EmailOutbox  # noqa: F401
from app.models.import_job import ImportBatch, ImportRow  # noqa: F401
from app.models.system_config import SystemConfig  # noqa: F401
from app.models.user import Account, Permission, Role, RolePermission, User  # noqa: F401

