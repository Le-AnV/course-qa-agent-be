"""Encrypt-at-rest helpers for admin-managed provider credentials."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from app.config import settings


class SecretEncryptionError(RuntimeError):
    """Raised when a stored credential cannot be decrypted with the active key."""


def _material() -> str:
    configured = settings.gemini_key_encryption_key
    if isinstance(configured, SecretStr):
        configured = configured.get_secret_value()
    return configured or settings.jwt_secret


def _fernet() -> Fernet:
    digest = hashlib.sha256(_material().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(value: str) -> str:
    """Return an opaque Fernet token; never store the raw provider key."""

    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    """Decrypt a stored token, surfacing a stable error for key rotation mistakes."""

    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as error:
        raise SecretEncryptionError(
            "Stored credential cannot be decrypted; check GEMINI_KEY_ENCRYPTION_KEY."
        ) from error


def secret_fingerprint(value: str) -> str:
    """Stable duplicate-detection hash (not a replacement for encryption)."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()
