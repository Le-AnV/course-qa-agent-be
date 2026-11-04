"""Router for Class Categories management (AI-91, Epic E6)."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, get_db
from app.models.user import User
from app.routers.class_config import _require_class_teacher
from app.schemas.category import (
    CategoryCreate,
    CategoryGenerateResponse,
    CategoryRead,
    CategoryTreeResponse,
    CategoryUpdate,
)
from app.services import category_service

router = APIRouter(prefix="/api/classes/{class_id}/categories", tags=["categories"])


@router.get("", response_model=CategoryTreeResponse)
def get_categories(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Retrieve 2-level category tree for a class."""
    _require_class_teacher(db, current_user, class_id)
    return category_service.list_categories(db, class_id)


@router.post("", response_model=CategoryRead, status_code=status.HTTP_201_CREATED)
def create_category(
    class_id: uuid.UUID,
    payload: CategoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Create a new Topic or Subtopic under this class."""
    _require_class_teacher(db, current_user, class_id)
    try:
        cat = category_service.create_category(db, class_id, payload)
        return CategoryRead.model_validate(cat)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.patch("/{category_id}", response_model=CategoryRead)
def update_category(
    class_id: uuid.UUID,
    category_id: uuid.UUID,
    payload: CategoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Rename or reorder a category."""
    _require_class_teacher(db, current_user, class_id)
    try:
        cat = category_service.update_category(db, class_id, category_id, payload)
        return CategoryRead.model_validate(cat)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/{category_id}")
def delete_category(
    class_id: uuid.UUID,
    category_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a category and its subcategories."""
    _require_class_teacher(db, current_user, class_id)
    deleted = category_service.delete_category(db, class_id, category_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Category not found.")
    return {"ok": True, "message": "Category deleted successfully"}


@router.post("/generate", response_model=CategoryGenerateResponse)
def generate_categories(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate 2-level category taxonomy from lecture materials using AI."""
    _require_class_teacher(db, current_user, class_id)
    try:
        cats = category_service.generate_categories_from_materials(db, class_id)
        return CategoryGenerateResponse(
            message="Categories successfully synthesized from class materials",
            generated_categories_count=len(cats),
            categories=[CategoryRead.model_validate(c) for c in cats],
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"AI generation failed: {exc}",
        ) from exc
