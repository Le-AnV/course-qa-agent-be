"""Router for Content Gap Analytics (AI-91, Phase 3)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.user import User
from app.routers.class_config import _require_class_teacher
from app.schemas.content_gap import ContentGapDashboardResponse
from app.services import content_gap_service

router = APIRouter(prefix="/api/classes/{class_id}/content-gaps", tags=["content-gaps"])


@router.get("", response_model=ContentGapDashboardResponse)
def get_content_gaps(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ContentGapDashboardResponse:
    """Retrieve Content Gap analytics dashboard with deduplication and reason grouping."""
    _require_class_teacher(db, current_user, class_id)
    return content_gap_service.compute_content_gap_dashboard(db, class_id, force_refresh=False)


@router.post("/refresh", response_model=ContentGapDashboardResponse)
def refresh_content_gaps(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> ContentGapDashboardResponse:
    """Force recomputation of Content Gap analytics, bypassing the 30-minute cache."""
    _require_class_teacher(db, current_user, class_id)
    content_gap_service.clear_dashboard_cache(class_id)
    return content_gap_service.compute_content_gap_dashboard(db, class_id, force_refresh=True)
