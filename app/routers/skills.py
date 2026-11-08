"""Router for Class-scoped Skill Taxonomy & Rubrics.

Allows teachers of a class (and Admins) to define skills and proficiency rubrics
specifically tailored for their course materials, syllabus, and quizzes.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.deps import get_current_user, get_db
from app.models.course import Class
from app.models.skill import Skill, SkillLevelRubric
from app.models.user import User
from app.schemas.skill import (
    SkillCreate,
    SkillLevelRubricRead,
    SkillLevelRubricUpdate,
    SkillRead,
    SkillUpdate,
)
from app.services import course_service, user_service
from app.services.skill_generation_service import generate_class_skill_taxonomy
from app.services.skill_taxonomy_service import normalize_skill_code

router = APIRouter(prefix="/api/classes/{class_id}/skills", tags=["class-skills"])

DEFAULT_THRESHOLDS = {
    "NOVICE": 80,
    "BEGINNER": 75,
    "COMPETENT": 70,
    "PROFICIENT": 65,
    "MASTER": 60,
}


def _require_class_teacher(db: Session, user: User, class_id: uuid.UUID) -> Class:
    """Allow admins, the primary trainer, or co-trainers of this class."""
    section = course_service.get_class(db, class_id)
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found.")
    role = (user_service.role_name_of(user) or "").upper()
    if role == "ADMIN":
        return section
    if role == "TRAINER" and section.id in course_service.class_ids_for_trainer(db, user.id):
        return section
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only an administrator or a trainer of this class can manage skills and rubrics.",
    )


@router.get("", response_model=list[SkillRead])
def list_class_skills(
    class_id: uuid.UUID,
    category: Annotated[str | None, Query(description="Filter by skill category")] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Skill]:
    """List all skills and level rubrics defined for a specific class."""
    _require_class_teacher(db, user, class_id)
    stmt = (
        select(Skill)
        .where(Skill.class_id == class_id)
        .options(selectinload(Skill.rubrics))
        .order_by(Skill.category, Skill.code)
    )
    if category:
        stmt = stmt.where(Skill.category == category.upper())
    return list(db.scalars(stmt).all())


@router.get("/{skill_id}", response_model=SkillRead)
def get_class_skill(
    class_id: uuid.UUID,
    skill_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Skill:
    """Retrieve details and rubrics of a skill within a class."""
    _require_class_teacher(db, user, class_id)
    skill = db.scalar(
        select(Skill)
        .where(Skill.id == skill_id, Skill.class_id == class_id)
        .options(selectinload(Skill.rubrics))
    )
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found in this class.")
    return skill


@router.post("", response_model=SkillRead, status_code=status.HTTP_201_CREATED)
def create_class_skill(
    class_id: uuid.UUID,
    payload: SkillCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Skill:
    """Create a new skill and its evaluation rubrics for this class."""
    _require_class_teacher(db, user, class_id)
    code = normalize_skill_code(payload.code)

    existing = db.scalar(
        select(Skill).where(Skill.class_id == class_id, Skill.code == code)
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Skill code '{code}' already exists for this class.",
        )

    skill = Skill(
        class_id=class_id,
        code=code,
        name=payload.name.strip(),
        category=payload.category.strip().upper(),
        description=payload.description,
    )
    db.add(skill)
    db.flush()

    # If teacher provided rubrics, persist them; otherwise initialize empty or default
    rubric_levels_added = set()
    for r in payload.rubrics:
        level_upper = r.level.upper()
        rubric = SkillLevelRubric(
            skill_id=skill.id,
            level=level_upper,
            description=r.description.strip(),
            threshold_pct=r.threshold_pct or DEFAULT_THRESHOLDS.get(level_upper, 70),
        )
        db.add(rubric)
        rubric_levels_added.add(level_upper)

    db.commit()
    db.refresh(skill)
    return skill


@router.patch("/{skill_id}", response_model=SkillRead)
def update_class_skill(
    class_id: uuid.UUID,
    skill_id: uuid.UUID,
    payload: SkillUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Skill:
    """Update skill metadata for this class."""
    _require_class_teacher(db, user, class_id)
    skill = db.scalar(
        select(Skill)
        .where(Skill.id == skill_id, Skill.class_id == class_id)
        .options(selectinload(Skill.rubrics))
    )
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found in this class.")

    if payload.name is not None:
        skill.name = payload.name.strip()
    if payload.category is not None:
        skill.category = payload.category.strip().upper()
    if payload.description is not None:
        skill.description = payload.description

    db.commit()
    db.refresh(skill)
    return skill


@router.put("/{skill_id}/rubrics/{level}", response_model=SkillLevelRubricRead)
def upsert_class_skill_rubric(
    class_id: uuid.UUID,
    skill_id: uuid.UUID,
    level: str,
    payload: SkillLevelRubricUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SkillLevelRubric:
    """Define or update what knowledge/scope is tested at this proficiency level."""
    _require_class_teacher(db, user, class_id)
    level_upper = level.upper()
    if level_upper not in DEFAULT_THRESHOLDS:
        raise HTTPException(status_code=400, detail="Invalid proficiency level.")

    skill = db.scalar(
        select(Skill).where(Skill.id == skill_id, Skill.class_id == class_id)
    )
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found in this class.")

    rubric = db.scalar(
        select(SkillLevelRubric).where(
            SkillLevelRubric.skill_id == skill_id,
            SkillLevelRubric.level == level_upper,
        )
    )

    if not rubric:
        if not payload.description:
            raise HTTPException(status_code=400, detail="Rubric description is required.")
        rubric = SkillLevelRubric(
            skill_id=skill_id,
            level=level_upper,
            description=payload.description.strip(),
            threshold_pct=payload.threshold_pct or DEFAULT_THRESHOLDS.get(level_upper, 70),
        )
        db.add(rubric)
    else:
        if payload.description is not None:
            rubric.description = payload.description.strip()
        if payload.threshold_pct is not None:
            rubric.threshold_pct = payload.threshold_pct

    db.commit()
    db.refresh(rubric)
    return rubric


@router.post(
    "/generate",
    response_model=list[SkillRead],
    status_code=status.HTTP_201_CREATED,
    summary="Auto-synthesize skill taxonomy and rubrics from class metadata",
)
def auto_generate_skills(
    class_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Skill]:
    """Auto-generate 3-7 skills with 5-level Dreyfus rubrics directly from class info.

    Uses class name, description, syllabus note, and academic level as the
    deterministic anchor to prevent taxonomy drift.
    """
    section = _require_class_teacher(db, user, class_id)

    class_desc_parts = []
    if section.info:
        if section.info.description:
            class_desc_parts.append(section.info.description)
        if section.info.note:
            class_desc_parts.append(section.info.note)
        if section.info.schedule_note:
            class_desc_parts.append(section.info.schedule_note)

    generated = generate_class_skill_taxonomy(
        class_name=section.name,
        class_code=section.code,
        credits=section.credits,
        semester=section.semester,
        academic_year=section.academic_year,
        class_description="\n".join(class_desc_parts) if class_desc_parts else None,
    )

    created_skills: list[Skill] = []
    for s_data in generated:
        code = s_data["code"]
        existing = db.scalar(
            select(Skill).where(Skill.class_id == class_id, Skill.code == code)
        )
        if existing:
            # Update description if empty
            if not existing.description and s_data.get("description"):
                existing.description = s_data["description"]
            skill_obj = existing
        else:
            skill_obj = Skill(
                class_id=class_id,
                code=code,
                name=s_data["name"],
                category=s_data.get("category", "GENERAL"),
                description=s_data.get("description"),
            )
            db.add(skill_obj)
            db.flush()

        # Add or update the 5-level rubrics
        for r in s_data.get("rubrics", []):
            lvl = r.get("level", "").upper()
            if lvl in DEFAULT_THRESHOLDS:
                rubric = db.scalar(
                    select(SkillLevelRubric).where(
                        SkillLevelRubric.skill_id == skill_obj.id,
                        SkillLevelRubric.level == lvl,
                    )
                )
                if not rubric:
                    db.add(
                        SkillLevelRubric(
                            skill_id=skill_obj.id,
                            level=lvl,
                            description=r.get("description", "").strip(),
                            threshold_pct=r.get("threshold_pct") or DEFAULT_THRESHOLDS[lvl],
                        )
                    )
                elif not rubric.description:
                    rubric.description = r.get("description", "").strip()

        created_skills.append(skill_obj)

    db.commit()
    return db.scalars(
        select(Skill)
        .where(Skill.class_id == class_id)
        .options(selectinload(Skill.rubrics))
    ).all()
