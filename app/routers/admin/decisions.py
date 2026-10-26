"""Admin endpoints for the OpenAI decisions key pool."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.config import settings
from app.core.deps import get_db, require_admin
from app.models.openai_key import OpenAIApiKey
from app.schemas.admin import (
    ConnectionTestResult,
    DecisionsKeyCreate,
    DecisionsKeyRead,
    DecisionsKeysRead,
    DecisionsKeyUpdate,
)
from app.services import openai_key_service

router = APIRouter()


def _read(key: OpenAIApiKey) -> DecisionsKeyRead:
    return DecisionsKeyRead(
        id=key.id,
        name=key.name,
        masked_key=openai_key_service.masked_key(key),
        status=openai_key_service.effective_status(key),
        priority=key.priority,
        cooldown_until=key.cooldown_until,
        last_error=key.last_error,
        last_used_at=key.last_used_at,
        created_at=key.created_at,
        updated_at=key.updated_at,
    )


def _load(db: Session, key_id: uuid.UUID) -> OpenAIApiKey:
    key = db.get(OpenAIApiKey, key_id)
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="OpenAI key not found."
        )
    return key


@router.get("/decisions/keys", response_model=DecisionsKeysRead)
def list_decisions_keys(db: Session = Depends(get_db), _=Depends(require_admin)):
    """List the decisions keys with masked values and effective status."""

    keys = (
        db.query(OpenAIApiKey)
        .order_by(OpenAIApiKey.priority.asc(), OpenAIApiKey.created_at.asc())
        .all()
    )
    return DecisionsKeysRead(
        keys=[_read(key) for key in keys],
        active_count=sum(
            1 for key in keys if openai_key_service.effective_status(key) == "active"
        ),
        env_configured=settings.openai_api_key is not None,
    )


@router.post(
    "/decisions/keys", response_model=DecisionsKeyRead, status_code=status.HTTP_201_CREATED
)
def create_decisions_key(
    payload: DecisionsKeyCreate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Validate a key against OpenAI, encrypt it, and add it to the pool."""

    try:
        key = openai_key_service.create_key(
            db,
            name=payload.name,
            api_key=payload.api_key,
            priority=payload.priority,
        )
    except openai_key_service.OpenAIApiKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error
    return _read(key)


@router.patch("/decisions/keys/{key_id}", response_model=DecisionsKeyRead)
def update_decisions_key(
    key_id: uuid.UUID,
    payload: DecisionsKeyUpdate,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Rename a key, change its priority, or pause/resume it."""

    key = _load(db, key_id)
    try:
        fields = payload.model_fields_set
        if fields & {"name", "priority"}:
            key = openai_key_service.update_key(
                db,
                key,
                name=payload.name,
                priority=payload.priority,
            )
        if payload.status is not None:
            key = openai_key_service.set_status(db, key, payload.status)
    except openai_key_service.OpenAIApiKeyError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from error
    return _read(key)


@router.delete("/decisions/keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_decisions_key(
    key_id: uuid.UUID,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Remove a key from the decisions pool."""

    openai_key_service.delete_key(db, _load(db, key_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/decisions/keys/{key_id}/test", response_model=ConnectionTestResult)
def test_decisions_key(
    key_id: uuid.UUID,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Check one stored key without exposing it or consuming generation quota."""

    key = _load(db, key_id)
    try:
        models = openai_key_service.validate_key(openai_key_service.decrypt_key(key))
    except openai_key_service.OpenAIApiKeyError as error:
        return ConnectionTestResult(success=False, message=str(error))
    return ConnectionTestResult(
        success=True,
        message=f"OpenAI key is valid ({len(models)} models visible).",
    )
