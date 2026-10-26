"""Admin endpoints for the Gemini multi-key pool and chat model selection."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_db, require_admin
from app.models.gemini_key import GeminiApiKey
from app.schemas.admin import (
    ConnectionTestResult,
    GeminiAutoSwitchRead,
    GeminiAutoSwitchUpdate,
    GeminiKeyCreate,
    GeminiKeyRead,
    GeminiKeysRead,
    GeminiKeyUpdate,
    GeminiModelConfigRead,
    GeminiModelConfigUpdate,
)
from app.services import gemini_key_service

router = APIRouter()


def _read(key: GeminiApiKey) -> GeminiKeyRead:
    return GeminiKeyRead(
        id=key.id,
        name=key.name,
        account_label=key.account_label,
        project_label=key.project_label,
        masked_key=gemini_key_service.masked_key(key),
        status=gemini_key_service.effective_status(key),
        priority=key.priority,
        available_models=list(key.available_models or []),
        cooldown_until=key.cooldown_until,
        last_error=key.last_error,
        last_used_at=key.last_used_at,
        created_at=key.created_at,
        updated_at=key.updated_at,
    )


def _load(db: Session, key_id: uuid.UUID) -> GeminiApiKey:
    key = db.get(GeminiApiKey, key_id)
    if key is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Gemini key not found.")
    return key


@router.get("/llm/keys", response_model=GeminiKeysRead)
def list_gemini_keys(db: Session = Depends(get_db), _=Depends(require_admin)):
    """List stored keys with masked values and effective status."""

    keys = (
        db.query(GeminiApiKey)
        .order_by(GeminiApiKey.priority.asc(), GeminiApiKey.created_at.asc())
        .all()
    )
    return GeminiKeysRead(
        keys=[_read(key) for key in keys],
        active_count=sum(
            1 for key in keys if gemini_key_service.effective_status(key) == "active"
        ),
        env_configured=settings.google_api_key is not None,
        auto_switch=gemini_key_service.get_auto_switch(db),
    )


@router.patch("/llm/auto-switch", response_model=GeminiAutoSwitchRead)
def update_gemini_auto_switch(
    payload: GeminiAutoSwitchUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Enable or disable automatic rotation when a key hits its limit."""

    return GeminiAutoSwitchRead(
        enabled=gemini_key_service.set_auto_switch(db, payload.enabled)
    )


@router.post("/llm/keys", response_model=GeminiKeyRead, status_code=status.HTTP_201_CREATED)
def create_gemini_key(
    payload: GeminiKeyCreate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Validate a key against Gemini, encrypt it, and add it to the pool."""

    try:
        key = gemini_key_service.create_key(
            db,
            name=payload.name,
            api_key=payload.api_key,
            account_label=payload.account_label,
            project_label=payload.project_label,
            priority=payload.priority,
        )
    except gemini_key_service.GeminiKeyError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return _read(key)


@router.patch("/llm/keys/{key_id}", response_model=GeminiKeyRead)
def update_gemini_key(
    key_id: uuid.UUID,
    payload: GeminiKeyUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Rename a key, change its labels/priority, or pause/resume it."""

    key = _load(db, key_id)
    try:
        fields = payload.model_fields_set
        if fields & {"name", "account_label", "project_label", "priority"}:
            account_label = payload.account_label
            if "account_label" in fields and account_label is None:
                account_label = ""
            project_label = payload.project_label
            if "project_label" in fields and project_label is None:
                project_label = ""
            key = gemini_key_service.update_key(
                db,
                key,
                name=payload.name,
                account_label=account_label,
                project_label=project_label,
                priority=payload.priority,
            )
        if payload.status is not None:
            key = gemini_key_service.set_status(db, key, payload.status)
    except gemini_key_service.GeminiKeyError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return _read(key)


@router.delete("/llm/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_gemini_key(
    key_id: uuid.UUID,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Remove a key from the pool; the audit trail stays in application logs."""

    gemini_key_service.delete_key(db, _load(db, key_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/llm/keys/{key_id}/test", response_model=ConnectionTestResult)
def test_gemini_key(
    key_id: uuid.UUID,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Check one stored key without exposing it or consuming generation quota."""

    key = _load(db, key_id)
    try:
        models = gemini_key_service.list_models(gemini_key_service.decrypt_key(key))
    except gemini_key_service.GeminiKeyError as error:
        return ConnectionTestResult(success=False, message=str(error))
    key.available_models = models
    db.commit()
    return ConnectionTestResult(
        success=True,
        message=f"Gemini key is valid ({len(models)} models available).",
    )


@router.get("/llm/models", response_model=GeminiModelConfigRead)
def get_gemini_models(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Return the active chat model and the models its keys can serve."""

    return GeminiModelConfigRead(
        model=gemini_key_service.get_chat_model(db),
        available_models=gemini_key_service.available_chat_models(db),
    )


@router.patch("/llm/model", response_model=GeminiModelConfigRead)
def update_gemini_model(
    payload: GeminiModelConfigUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Persist the chat model used for new answers."""

    try:
        gemini_key_service.set_chat_model(db, payload.model)
    except gemini_key_service.GeminiKeyError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)) from error
    return GeminiModelConfigRead(
        model=gemini_key_service.get_chat_model(db),
        available_models=gemini_key_service.available_chat_models(db),
    )
