"""Pydantic schemas for auth (E1 / AI-25)."""

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
