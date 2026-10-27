"""Gemini multi-key registry: validation, encrypted storage, status, and selection."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from google import genai
from sqlalchemy.orm import Session

from app.config import settings
from app.core.crypto import (
    SecretEncryptionError,
    decrypt_secret,
    encrypt_secret,
    secret_fingerprint,
)
from app.models.gemini_key import (
    GEMINI_KEY_ACTIVE,
    GEMINI_KEY_INVALID,
    GEMINI_KEY_PAUSED,
    GeminiApiKey,
)
from app.models.system_config import SystemConfig

STATUS_ACTIVE = GEMINI_KEY_ACTIVE
STATUS_PAUSED = GEMINI_KEY_PAUSED
STATUS_INVALID = GEMINI_KEY_INVALID
STATUS_COOLDOWN = "cooldown"
STATUSES = (STATUS_ACTIVE, STATUS_PAUSED, STATUS_INVALID)

CHAT_MODEL_CONFIG_KEY = "gemini_chat_model"
AUTO_SWITCH_CONFIG_KEY = "gemini_auto_switch"
FALLBACK_CHAT_MODEL = "models/gemini-3.1-flash-lite"
PACIFIC = ZoneInfo("America/Los_Angeles")
DEFAULT_COOLDOWN_SECONDS = 60.0
QUOTA_EXHAUSTED_COOLDOWN_SECONDS = 86_400.0
TIMEOUT_COOLDOWN_SECONDS = 30.0
MAX_ERROR_LENGTH = 1000

_RETRY_PATTERNS = (
    re.compile(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'"),
    re.compile(r"retry in (\d+(?:\.\d+)?)s", re.IGNORECASE),
)


class GeminiKeyError(ValueError):
    """A key cannot be stored, selected, or used safely."""


@dataclass(frozen=True)
class ResolvedGeminiKey:
    """A usable plaintext key selected for one provider call."""

    api_key: str
    key_id: uuid.UUID | None
    key_name: str
    key: GeminiApiKey | None = None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _now(value: datetime | None = None) -> datetime:
    return _utc(value) or datetime.now(UTC)


def _next_day_start(value: datetime) -> datetime:
    local = value.astimezone(PACIFIC)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return start.astimezone(UTC)


def model_id(model: str) -> str:
    return model.rsplit("/", maxsplit=1)[-1].strip().lower()


def _is_chat_model(model: str) -> bool:
    name = model_id(model)
    if not name.startswith("gemini"):
        return False
    blocked = (
        "audio",
        "computer-use",
        "computer_use",
        "deep-research",
        "embedding",
        "image",
        "imagen",
        "live",
        "lyria",
        "music",
        "nano-banana",
        "robotics",
        "transcribe",
        "tts",
        "veo",
        "video",
    )
    return not any(token in name for token in blocked)


def list_models(api_key: str) -> list[str]:
    """Validate a key against models.list() and return the full model ids."""

    try:
        with genai.Client(api_key=api_key) as client:
            names = [model.name for model in client.models.list() if model.name]
    except Exception as error:
        raise GeminiKeyError("Gemini rejected this API key.") from error
    if not names:
        raise GeminiKeyError("Gemini returned no models for this API key.")
    return names


def create_key(
    db: Session,
    *,
    name: str,
    api_key: str,
    account_label: str | None = None,
    project_label: str | None = None,
    priority: int = 100,
) -> GeminiApiKey:
    """Validate, encrypt, and persist a new Gemini key."""

    name = name.strip()
    api_key = api_key.strip()
    if not name:
        raise GeminiKeyError("Key name is required.")
    if not api_key:
        raise GeminiKeyError("Gemini API key is required.")
    if db.query(GeminiApiKey).filter(GeminiApiKey.name == name).first():
        raise GeminiKeyError(f"A Gemini key named '{name}' already exists.")
    fingerprint = secret_fingerprint(api_key)
    if db.query(GeminiApiKey).filter(GeminiApiKey.key_fingerprint == fingerprint).first():
        raise GeminiKeyError("This Gemini API key already exists.")

    models = list_models(api_key)
    key = GeminiApiKey(
        name=name,
        account_label=(account_label or "").strip() or None,
        project_label=(project_label or "").strip() or None,
        encrypted_key=encrypt_secret(api_key),
        key_prefix=api_key[:4],
        key_last4=api_key[-4:],
        key_fingerprint=fingerprint,
        available_models=models,
        status=STATUS_ACTIVE,
        priority=priority,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


def update_key(
    db: Session,
    key: GeminiApiKey,
    *,
    name: str | None = None,
    account_label: str | None = None,
    project_label: str | None = None,
    priority: int | None = None,
) -> GeminiApiKey:
    """Update non-secret metadata."""

    if name is not None:
        cleaned = name.strip()
        if not cleaned:
            raise GeminiKeyError("Key name is required.")
        duplicate = (
            db.query(GeminiApiKey)
            .filter(GeminiApiKey.name == cleaned, GeminiApiKey.id != key.id)
            .first()
        )
        if duplicate:
            raise GeminiKeyError(f"A Gemini key named '{cleaned}' already exists.")
        key.name = cleaned
    if account_label is not None:
        key.account_label = account_label.strip() or None
    if project_label is not None:
        key.project_label = project_label.strip() or None
    if priority is not None:
        key.priority = priority
    db.commit()
    db.refresh(key)
    return key


def set_status(db: Session, key: GeminiApiKey, status: str) -> GeminiApiKey:
    """Pause, resume, or mark a key invalid."""

    if status not in STATUSES:
        raise GeminiKeyError(f"Unknown Gemini key status: {status}")
    key.status = status
    if status == STATUS_ACTIVE:
        key.cooldown_until = None
        key.last_error = ""
    db.commit()
    db.refresh(key)
    return key


def delete_key(db: Session, key: GeminiApiKey) -> None:
    db.delete(key)
    db.commit()


def decrypt_key(key: GeminiApiKey) -> str:
    try:
        return decrypt_secret(key.encrypted_key)
    except SecretEncryptionError as error:
        raise GeminiKeyError(str(error)) from error


def masked_key(key: GeminiApiKey) -> str:
    return f"{key.key_prefix}...{key.key_last4}"


def effective_status(key: GeminiApiKey, *, now: datetime | None = None) -> str:
    """Status shown to admins: cooldown is temporary, paused/invalid are deliberate."""

    if key.status != STATUS_ACTIVE:
        return key.status
    cooldown = _utc(key.cooldown_until)
    if cooldown is not None and cooldown > _now(now):
        return STATUS_COOLDOWN
    return STATUS_ACTIVE


def touch_key(db: Session, key: GeminiApiKey, *, now: datetime | None = None) -> None:
    """Mark a successful use and clear an expired cooldown/error."""

    key.last_used_at = _now(now)
    key.cooldown_until = None
    key.last_error = ""
    db.commit()


def _supports_model(key: GeminiApiKey, model: str) -> bool:
    if not key.available_models:
        return True
    wanted = model_id(model)
    return any(model_id(candidate) == wanted for candidate in key.available_models)


def select_key(
    db: Session,
    *,
    model: str,
    now: datetime | None = None,
    exclude_ids: set[uuid.UUID] | None = None,
    preferred_id: uuid.UUID | None = None,
) -> GeminiApiKey | None:
    """Choose the highest-priority healthy key that can serve ``model``."""

    current = _now(now)
    excluded = exclude_ids or set()
    candidates = (
        db.query(GeminiApiKey)
        .filter(GeminiApiKey.status == STATUS_ACTIVE)
        .order_by(GeminiApiKey.priority.asc(), GeminiApiKey.created_at.asc())
        .all()
    )
    eligible: list[GeminiApiKey] = []
    for key in candidates:
        if key.id in excluded:
            continue
        cooldown = _utc(key.cooldown_until)
        if cooldown is not None and cooldown > current:
            continue
        if not _supports_model(key, model):
            continue
        eligible.append(key)

    eligible.sort(
        key=lambda key: (
            0 if preferred_id is not None and key.id == preferred_id else 1,
            key.priority,
            _utc(key.last_used_at) or datetime.min.replace(tzinfo=UTC),
            _utc(key.created_at) or datetime.min.replace(tzinfo=UTC),
            str(key.id),
        )
    )
    return eligible[0] if eligible else None


def resolve_key(
    db: Session,
    *,
    model: str,
    now: datetime | None = None,
    exclude_ids: set[uuid.UUID] | None = None,
    preferred_id: uuid.UUID | None = None,
) -> ResolvedGeminiKey | None:
    """Resolve a DB key first, then fall back to GOOGLE_API_KEY/GEMINI_API_KEY."""

    key = select_key(
        db,
        model=model,
        now=now,
        exclude_ids=exclude_ids,
        preferred_id=preferred_id,
    )
    if key is not None:
        return ResolvedGeminiKey(
            api_key=decrypt_key(key),
            key_id=key.id,
            key_name=key.name,
            key=key,
        )
    env_key = settings.google_api_key
    if env_key is not None:
        raw = env_key.get_secret_value() if hasattr(env_key, "get_secret_value") else env_key
        if raw:
            return ResolvedGeminiKey(api_key=str(raw), key_id=None, key_name="Environment")
    return None


def _retry_delay_seconds(message: str) -> float | None:
    for pattern in _RETRY_PATTERNS:
        match = pattern.search(message)
        if match:
            return float(match.group(1))
    return None


def mark_key_failure(
    db: Session,
    key: GeminiApiKey,
    error: str | BaseException,
    *,
    now: datetime | None = None,
) -> None:
    """Disable invalid keys and cool down rate-limited/transient ones."""

    current = _now(now)
    message = str(error)
    lowered = message.lower()
    key.last_error = message[:MAX_ERROR_LENGTH]
    invalid_markers = (
        "api key not valid",
        "api key is invalid",
        "api_key_invalid",
        "unauthenticated",
        " 401",
        "invalid_argument",
    )
    if any(marker in lowered for marker in invalid_markers):
        key.status = STATUS_INVALID
        key.cooldown_until = None
    else:
        quota_markers = (
            "insufficient_quota",
            "credit_balance_exhausted",
            "no credits remaining",
            "exceeded your current quota",
        )
        if any(marker in lowered for marker in quota_markers):
            seconds = QUOTA_EXHAUSTED_COOLDOWN_SECONDS
        elif "per day" in lowered or "daily" in lowered:
            seconds = max((_next_day_start(current) - current).total_seconds(), 1.0)
        elif isinstance(error, TimeoutError) or "timed out" in lowered:
            seconds = TIMEOUT_COOLDOWN_SECONDS
        else:
            seconds = _retry_delay_seconds(message) or DEFAULT_COOLDOWN_SECONDS
        key.cooldown_until = current + timedelta(seconds=seconds)
    db.commit()


def get_chat_model(db: Session) -> str:
    """Active chat model from SystemConfig, falling back to the code default."""

    row = db.get(SystemConfig, CHAT_MODEL_CONFIG_KEY)
    value = (row.value if row else "").strip()
    return value or settings.llm_model


def available_chat_models(db: Session) -> list[str]:
    """Union of chat-capable models visible to the admin's active keys."""

    models: set[str] = set()
    for key in db.query(GeminiApiKey).filter(GeminiApiKey.status == STATUS_ACTIVE).all():
        for model in key.available_models or []:
            if _is_chat_model(model):
                models.add(model)
    models.add(settings.llm_model)
    models.add(FALLBACK_CHAT_MODEL)
    return sorted(models)


def set_chat_model(db: Session, model: str) -> str:
    """Persist the chat model only when at least one active key exposes it."""

    model = model.strip()
    if not model:
        raise GeminiKeyError("Chat model is required.")
    known = {model_id(candidate) for candidate in available_chat_models(db)}
    if model_id(model) not in known:
        raise GeminiKeyError(f"Model '{model}' is not available on any active Gemini key.")
    row = db.get(SystemConfig, CHAT_MODEL_CONFIG_KEY)
    if row is None:
        row = SystemConfig(key=CHAT_MODEL_CONFIG_KEY, value=model)
        db.add(row)
    else:
        row.value = model
    db.commit()
    return model


def get_auto_switch(db: Session) -> bool:
    """Whether a failed key should rotate to the next healthy key."""

    row = db.get(SystemConfig, AUTO_SWITCH_CONFIG_KEY)
    if row is None:
        return True
    return row.value.strip().lower() not in {"0", "false", "no", "off"}


def set_auto_switch(db: Session, enabled: bool) -> bool:
    """Persist the admin's auto-switch preference."""

    row = db.get(SystemConfig, AUTO_SWITCH_CONFIG_KEY)
    value = "true" if enabled else "false"
    if row is None:
        db.add(SystemConfig(key=AUTO_SWITCH_CONFIG_KEY, value=value))
    else:
        row.value = value
    db.commit()
    return enabled
