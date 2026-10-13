"""Pydantic schemas for auth — DB login (E1 / AI-25, AI-27, AI-31) + mock login compat."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    # Lowercase role alias for FE compat (DB stores UPPERCASE).
    role: str
    user_id: uuid.UUID
    # Compat name — sourced from ``user.full_name`` (ERD has no username column).
    username: str
    class_id: uuid.UUID | None = None


class MeResponse(BaseModel):
    id: uuid.UUID
    # Compat name — sourced from ``user.full_name``.
    username: str
    email: str
    role: str
    class_id: uuid.UUID | None
    is_active: bool


class MockLoginRequest(BaseModel):
    """Compat with the pre-DB mock login used by the FE login modal (/auth/login)."""

    identifier: str
    password: str


class MockLoginResponse(BaseModel):
    token: str
    role: str


class ProfileUpdateRequest(BaseModel):
    """AC-03: the caller edits only their own display name (user.full_name)."""

    full_name: str = Field(min_length=1, max_length=100)


class ChangePasswordRequest(BaseModel):
    """AC-02: old password must be verified before the hash is replaced."""

    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class MessageResponse(BaseModel):
    message: str
