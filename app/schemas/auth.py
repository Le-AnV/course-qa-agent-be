"""Pydantic schemas for auth — DB login (E1 / AI-25, AI-27, AI-31) + mock login compat."""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    user_id: int
    username: str
    class_id: int | None = None


class MeResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str
    class_id: int | None
    is_active: bool


class MockLoginRequest(BaseModel):
    """Compat with the pre-DB mock login used by the FE login modal (/auth/login)."""

    identifier: str
    password: str


class MockLoginResponse(BaseModel):
    token: str
    role: str
