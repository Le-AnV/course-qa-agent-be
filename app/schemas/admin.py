"""Response contracts for Admin system settings and connection checks."""

from typing import Literal

from pydantic import BaseModel, EmailStr, Field


class LlmSettingsRead(BaseModel):
    """Expose the active model and whether its API key is configured."""

    model: str = Field(min_length=1)
    api_key_configured: bool


class EmailSettingsRead(BaseModel):
    """Expose SMTP metadata without returning the app password."""

    smtp_host: str = Field(min_length=1)
    smtp_port: int = Field(ge=1, le=65535)
    smtp_security: Literal["STARTTLS"]
    sender_email: EmailStr | None
    rate_limit_per_minute: int = Field(ge=1)
    app_password_configured: bool


class AdminSettingsRead(BaseModel):
    """Group AI-105 LLM and email settings displayed to an Admin."""

    llm: LlmSettingsRead
    email: EmailSettingsRead


class ConnectionTestResult(BaseModel):
    """Report an external connection check without exposing credentials."""

    success: bool
    message: str = Field(min_length=1)
