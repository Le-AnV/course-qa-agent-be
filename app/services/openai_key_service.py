"""OpenAI decisions multi-key registry: validation, storage, and selection."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import (
    SecretEncryptionError,
    decrypt_secret,
    encrypt_secret,
    secret_fingerprint,
)
from app.models.openai_key import (
    OPENAI_KEY_ACTIVE,
    OPENAI_KEY_INVALID,
    OPENAI_KEY_PAUSED,
    OpenAIApiKey,
)

STATUS_ACTIVE = OPENAI_KEY_ACTIVE
STATUS_PAUSED = OPENAI_KEY_PAUSED
STATUS_INVALID = OPENAI_KEY_INVALID
STATUS_COOLDOWN = "cooldown"
STATUSES = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_INVALID)

MODELS_URL = "https://api.openai.com/v1/models"
DEFAULT_COOLDOWN_SECONDS = 60.0
MAX_ERROR_LENGTH = 1000

_RETRY_PATTERNS = (
    re.compile(r"retry.after[:\s]+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE),
    re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE),
)


class OpenAIApiKeyError(ValueError):
    """A key cannot be stored, selected, or used safely."""


@dataclass(frozen=True)
class ResolvedOpenAIKey:
    """A usable plaintext key selected for one Decisions call."""

    api_key: str
    key_id: uuid.UUID | None
    key_name: str
    key: OpenAIApiKey | None = None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _now(value: datetime | None = None) -> datetime:
    return _utc(value) or datetime.now(UTC)


def validate_key(api_key: str) -> list[str]:
    """Validate a key against models.list() and return the visible model ids."""

    try:
        response = httpx.get(
            MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(8.0, connect=3.0),
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise OpenAIApiKeyError("OpenAI rejected this API key.") from error
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise OpenAIApiKeyError("OpenAI returned an unexpected response for this key.")
    return [
        item["id"]
        for item in data
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]


def create_key(
    db: Session,
    *,
    name: str,
    api_key: str,
    priority: int = 100,
) -> OpenAIApiKey:
    """Validate, encrypt, and persist a new OpenAI key."""

    name = name.strip()
    api_key = api_key.strip()
    if not name:
        raise OpenAIApiKeyError("Key name is required.")
    if not api_key:
        raise OpenAIApiKeyError("OpenAI API key is required.")
    if db.query(OpenAIApiKey).filter(OpenAIApiKey.name == name).first():
        raise OpenAIApiKeyError(f"An OpenAI key named '{name}' already exists.")
    fingerprint = secret_fingerprint(api_key)
    if db.query(OpenAIApiKey).filter(OpenAIApiKey.key_fingerprint == fingerprint).first():
        raise OpenAIApiKeyError("This OpenAI API key already exists.")

    validate_key(api_key)
    key = OpenAIApiKey(
        name=name,
        encrypted_key=encrypt_secret(api_key),
        key_prefix=api_key[:4],
        key_last4=api_key[-4:],
        key_fingerprint=fingerprint,
        status=STATUS_ACTIVE,
        priority=priority,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


def update_key(
    db: Session,
    key: OpenAIApiKey,
    *,
    name: str | None = None,
    priority: int | None = None,
) -> OpenAIApiKey:
    """Update non-secret metadata."""

    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise OpenAIApiKeyError("Key name is required.")
        duplicate = (
            db.query(OpenAIApiKey)
            .filter(OpenAIApiKey.name == cleaned, OpenAIApiKey.id != key.id)
            .first()
        )
        if duplicate:
            raise OpenAIApiKeyError(f"An OpenAI key named '{cleaned}' already exists.")
        key.name = cleaned
    if priority is not None:
        key.priority = priority
    db.commit()
    db.refresh(key)
    return key


def set_status(db: Session, key: OpenAIApiKey, status: str) -> OpenAIApiKey:
    """Pause, resume, or mark a key invalid."""

    if status not in STATUSES:
        raise OpenAIApiKeyError(f"Unknown OpenAI key status: {status}")
    key.status = status
    if status == STATUS_ACTIVE:
        key.cooldown_until = None
        key.last_error = ""
    db.commit()
    db.refresh(key)
    return key


def delete_key(db: Session, key: OpenAIApiKey) -> None:
    db.delete(key)
    db.commit()


def decrypt_key(key: OpenAIApiKey) -> str:
    try:
        return decrypt_secret(key.encrypted_key)
    except SecretEncryptionError as error:
        raise OpenAIApiKeyError(str(error)) from error


def masked_key(key: OpenAIApiKey) -> str:
    return f"{key.key_prefix}...{key.key_last4}"


def effective_status(key: OpenAIApiKey, *, now: datetime | None = None) -> str:
    """Status shown to admins: cooldown is temporary, paused/invalid are deliberate."""

    if key.status != STATUS_ACTIVE:
        return key.status
    cooldown = _utc(key.cooldown_until)
    if cooldown is not None and cooldown > _now(now):
        return STATUS_COOLDOWN
    return STATUS_ACTIVE


def touch_key(db: Session, key: OpenAIApiKey, *, now: datetime | None = None) -> None:
    """Mark a successful use and clear an expired cooldown/error."""

    key.last_used_at = _now(now)
    key.cooldown_until = None
    key.last_error = ""
    db.commit()


def select_key(
    db: Session,
    *,
    now: datetime | None = None,
    exclude_ids: set[uuid.UUID] | None = None,
) -> OpenAIApiKey | None:
    """Choose the highest-priority healthy key, least recently used first."""

    current = _now(now)
    excluded = exclude_ids or set()
    candidates = (
        db.query(OpenAIApiKey)
        .filter(OpenAIApiKey.status == STATUS_ACTIVE)
        .order_by(OpenAIApiKey.priority.asc(), OpenAIApiKey.created_at.asc())
        .all()
    )
    eligible: list[OpenAIApiKey] = []
    for key in candidates:
        if key.id in excluded:
            continue
        cooldown = _utc(key.cooldown_until)
        if cooldown is not None and cooldown > current:
            continue
        eligible.append(key)

    eligible.sort(
        key=lambda key: (
            key.priority,
            _utc(key.last_used_at) or datetime.min.replace(tzinfo=UTC),
            _utc(key.created_at) or datetime.min.replace(tzinfo=UTC),
            str(key.id),
        )
    )
    return eligible[0] if eligible else None


def resolve_key(
    db: Session | None,
    *,
    now: datetime | None = None,
    exclude_ids: set[uuid.UUID] | None = None,
) -> ResolvedOpenAIKey | None:
    """Resolve a DB key first, then fall back to the OPENAI_API_KEY setting."""

    if db is not None:
        key = select_key(db, now=now, exclude_ids=exclude_ids)
        if key is not None:
            return ResolvedOpenAIKey(
                api_key=decrypt_key(key),
                key_id=key.id,
                key_name=key.name,
                key=key,
            )
    if exclude_ids:
        return None
    env_key = settings.openai_api_key
    if env_key is not None:
        raw = env_key.get_secret_value() if hasattr(env_key, "get_secret_value") else env_key
        if raw:
            return ResolvedOpenAIKey(api_key=str(raw), key_id=None, key_name="Environment")
    return None


def _cooldown_seconds(message: str) -> float:
    for pattern in _RETRY_PATTERNS:
        match = pattern.search(message)
        if match:
            return float(match.group(1))
    return DEFAULT_COOLDOWN_SECONDS


def mark_key_failure(
    db: Session,
    key: OpenAIApiKey,
    error: str | BaseException,
    *,
    now: datetime | None = None,
) -> None:
    """Disable rejected keys and cool down rate-limited/transient ones."""

    current = _now(now)
    message = str(error)
    lowered = message.lower()
    key.last_error = message[:MAX_ERROR_LENGTH]
    invalid_markers = (
        "401",
        "403",
        "incorrect api key",
        "invalid api key",
        "invalid_api_key",
        "unauthorized",
    )
    if any(marker in lowered for marker in invalid_markers):
        key.status = STATUS_INVALID
        key.cooldown_until = None
    else:
        key.cooldown_until = current + timedelta(seconds=_cooldown_seconds(message))
    db.commit()
