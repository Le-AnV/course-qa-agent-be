"""Admin-only endpoints for AI-105 runtime settings and connection checks."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.config import settings
from app.dependencies.auth import require_admin
from app.schemas.admin import AdminSettingsRead, ConnectionTestResult
from app.services.admin_settings_service import build_admin_settings
from app.services.email_service import (
    EmailConfigurationError,
    EmailDeliveryError,
    send_test_email,
)
from app.services.llm_service import (
    LlmConfigurationError,
    LlmConnectionError,
    check_llm_connection,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/settings", response_model=AdminSettingsRead)
def read_admin_settings(user: Annotated[dict[str, str], Depends(require_admin)]):
    """Return active configuration metadata without exposing credentials."""

    return build_admin_settings(settings)


@router.post("/llm/test-connection", response_model=ConnectionTestResult)
def test_llm_connection(user: Annotated[dict[str, str], Depends(require_admin)]):
    """Check the configured Gemini key and model, returning a safe result."""

    try:
        return check_llm_connection(settings)
    except (LlmConfigurationError, LlmConnectionError) as error:
        return ConnectionTestResult(success=False, message=str(error))


@router.post("/email/test", response_model=ConnectionTestResult)
def test_email_sender(user: Annotated[dict[str, str], Depends(require_admin)]):
    """Send a test message to the authenticated Admin's email address."""

    try:
        return send_test_email(user["identifier"], settings)
    except (EmailConfigurationError, EmailDeliveryError) as error:
        return ConnectionTestResult(success=False, message=str(error))
