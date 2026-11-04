"""Class Category service for 2-level knowledge taxonomy (AI-91, Epic E6).

Manages:
- 2-level hierarchy in PostgreSQL (Topic -> Subtopic)
- Category embedding synchronization in Qdrant collection 'class_categories'
- AI-driven category extraction from approved class materials
"""

import json
import logging
import re
import uuid
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from qdrant_client.http import models as qmodels
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config.config import settings
from app.core.prompt_manager import get_prompt_manager
from app.models.category import ClassCategory
from app.models.course import Class
from app.models.document import Document
from app.schemas.category import CategoryCreate, CategoryRead, CategoryTreeResponse, CategoryUpdate
from app.services.embedding.gemini_embedding import GeminiEmbedding
from app.services.history_service import get_qdrant_client
from app.services.llm.provider import get_llm_model

logger = logging.getLogger(__name__)

CATEGORY_COLLECTION = "class_categories"


def ensure_category_collection(client: Any) -> None:
    """Ensure the class_categories collection exists in Qdrant (AC-06)."""
    try:
        if not client.collection_exists(CATEGORY_COLLECTION):
            client.create_collection(
                collection_name=CATEGORY_COLLECTION,
                vectors_config=qmodels.VectorParams(
                    size=settings.vector_size, distance=qmodels.Distance.COSINE
                ),
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to ensure collection '%s' in Qdrant: %s", CATEGORY_COLLECTION, exc)


def sync_category_vector(
    category_id: str,
    class_id: str,
    name: str,
    *,
    parent_id: str | None = None,
    embedding: list[float] | None = None,
    qdrant_client: Any | None = None,
) -> None:
    """Upsert category embedding into Qdrant collection 'class_categories'."""
    if not embedding:
        return
    client = qdrant_client or get_qdrant_client()
    if client is None:
        return

    try:
        ensure_category_collection(client)
        point = qmodels.PointStruct(
            id=str(category_id),
            vector=embedding,
            payload={
                "category_id": str(category_id),
                "class_id": str(class_id),
                "parent_id": str(parent_id) if parent_id else None,
                "name": str(name),
            },
        )
        client.upsert(collection_name=CATEGORY_COLLECTION, points=[point])
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to sync category vector to Qdrant: %s", exc)


def delete_category_vector(
    category_ids: list[str],
    qdrant_client: Any | None = None,
) -> None:
    """Delete category points from Qdrant by ID."""
    if not category_ids:
        return
    client = qdrant_client or get_qdrant_client()
    if client is None:
        return

    try:
        client.delete(
            collection_name=CATEGORY_COLLECTION,
            points_selector=qmodels.PointIdsList(points=category_ids),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to delete category points from Qdrant: %s", exc)


def list_categories(db: Session, class_id: uuid.UUID) -> CategoryTreeResponse:
    """Fetch 2-level category tree for a class."""
    # Top-level (Level 1) categories
    level1_cats = db.scalars(
        select(ClassCategory)
        .where(ClassCategory.class_id == class_id, ClassCategory.parent_id.is_(None))
        .order_by(ClassCategory.name.asc())
    ).all()

    total_chunks = (
        db.scalar(
            select(func.coalesce(func.sum(ClassCategory.chunk_count), 0)).where(
                ClassCategory.class_id == class_id
            )
        )
        or 0
    )

    total_count = (
        db.scalar(
            select(func.count(ClassCategory.id)).where(ClassCategory.class_id == class_id)
        )
        or 0
    )

    items = [CategoryRead.model_validate(c) for c in level1_cats]
    return CategoryTreeResponse(
        items=items,
        total_categories=total_count,
        total_chunks=total_chunks,
    )


def create_category(
    db: Session,
    class_id: uuid.UUID,
    payload: CategoryCreate,
    qdrant_client: Any | None = None,
) -> ClassCategory:
    """Create a new category (enforces 2-level maximum hierarchy)."""
    parent_id = payload.parent_id
    if parent_id is not None:
        parent = db.scalar(
            select(ClassCategory).where(
                ClassCategory.id == parent_id, ClassCategory.class_id == class_id
            )
        )
        if not parent:
            raise ValueError("Parent category not found in this class.")
        if parent.parent_id is not None:
            raise ValueError("Cannot create subcategory under a subcategory (maximum 2 levels).")

    cat = ClassCategory(
        class_id=class_id,
        parent_id=parent_id,
        name=payload.name.strip(),
        chunk_count=0,
        status="active",
    )
    db.add(cat)
    db.commit()
    db.refresh(cat)

    # Embed and sync to Qdrant
    try:
        embed_text = f"{parent.name} - {cat.name}" if parent_id else cat.name
        vector = GeminiEmbedding(db=db).embed_query(embed_text)
        sync_category_vector(
            category_id=str(cat.id),
            class_id=str(class_id),
            name=cat.name,
            parent_id=str(parent_id) if parent_id else None,
            embedding=vector,
            qdrant_client=qdrant_client,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to embed new category '%s': %s", cat.name, exc)

    return cat


def update_category(
    db: Session,
    class_id: uuid.UUID,
    category_id: uuid.UUID,
    payload: CategoryUpdate,
    qdrant_client: Any | None = None,
) -> ClassCategory:
    """Rename or move a category."""
    cat = db.scalar(
        select(ClassCategory).where(
            ClassCategory.id == category_id, ClassCategory.class_id == class_id
        )
    )
    if not cat:
        raise ValueError("Category not found.")

    if payload.name is not None:
        cat.name = payload.name.strip()

    if payload.parent_id is not None:
        if payload.parent_id == cat.id:
            raise ValueError("Category cannot be its own parent.")
        parent = db.scalar(
            select(ClassCategory).where(
                ClassCategory.id == payload.parent_id, ClassCategory.class_id == class_id
            )
        )
        if not parent or parent.parent_id is not None:
            raise ValueError("Invalid parent category (must be a Level 1 topic).")
        cat.parent_id = payload.parent_id

    db.commit()
    db.refresh(cat)

    # Re-embed and sync
    try:
        vector = GeminiEmbedding(db=db).embed_query(cat.name)
        sync_category_vector(
            category_id=str(cat.id),
            class_id=str(class_id),
            name=cat.name,
            parent_id=str(cat.parent_id) if cat.parent_id else None,
            embedding=vector,
            qdrant_client=qdrant_client,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to re-embed category '%s': %s", cat.name, exc)

    return cat


def delete_category(
    db: Session,
    class_id: uuid.UUID,
    category_id: uuid.UUID,
    qdrant_client: Any | None = None,
) -> bool:
    """Delete a category and its subcategories."""
    cat = db.scalar(
        select(ClassCategory).where(
            ClassCategory.id == category_id, ClassCategory.class_id == class_id
        )
    )
    if not cat:
        return False

    # Collect IDs to delete from Qdrant
    ids_to_delete = [str(cat.id)]
    for sub in cat.subcategories:
        ids_to_delete.append(str(sub.id))

    db.delete(cat)
    db.commit()

    delete_category_vector(ids_to_delete, qdrant_client=qdrant_client)
    return True


def generate_categories_from_materials(
    db: Session,
    class_id: uuid.UUID,
    qdrant_client: Any | None = None,
) -> list[ClassCategory]:
    """Generate 2-level category taxonomy from approved class documents using Gemini (AC-04)."""
    clazz = db.get(Class, class_id)
    if not clazz:
        raise ValueError("Class not found.")

    class_info = clazz.info
    class_description = class_info.description if class_info and class_info.description else clazz.name

    # Fetch document titles and summaries
    docs = db.scalars(
        select(Document).where(Document.class_id == class_id)
    ).all()

    materials_summary = "\n".join([f"- Document: {d.filename} (Title: {d.title or d.filename})" for d in docs])
    if not materials_summary:
        materials_summary = "No uploaded lecture documents found yet. Base the initial curriculum on the subject title and description."

    prompt_mgr = get_prompt_manager()
    prompt_config = prompt_mgr.get_prompt("class_material_categories.yaml", "material-categories-prompt")

    system_prompt = prompt_config.get("system-prompt", "")
    user_prompt_template = prompt_config.get("user-prompt", "")
    user_prompt = user_prompt_template.format(
        class_name=clazz.name,
        class_description=class_description,
        materials_summary=materials_summary,
    )

    llm = get_llm_model()
    response = llm.invoke([SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)])
    content = str(response.content)

    # Parse JSON from response
    match = re.search(r"\{[\s\S]*\}", content)
    if not match:
        raise ValueError("Failed to parse category JSON from AI response.")

    data = json.loads(match.group(0))
    raw_categories = data.get("categories", [])

    created_level1: list[ClassCategory] = []
    embedding_model = GeminiEmbedding(db=db)

    for item in raw_categories:
        topic_name = item.get("name", "").strip()
        if not topic_name:
            continue

        level1 = ClassCategory(
            class_id=class_id,
            parent_id=None,
            name=topic_name,
            chunk_count=0,
            status="auto_generated",
        )
        db.add(level1)
        db.flush()

        try:
            vector1 = embedding_model.embed_query(topic_name)
            sync_category_vector(
                category_id=str(level1.id),
                class_id=str(class_id),
                name=topic_name,
                embedding=vector1,
                qdrant_client=qdrant_client,
            )
        except Exception:  # noqa: BLE001
            logger.warning("Could not embed level 1 category '%s'", topic_name)

        # Subcategories
        subtopics = item.get("subcategories", [])
        for sub_name in subtopics:
            sub_name = str(sub_name).strip()
            if not sub_name:
                continue
            sub = ClassCategory(
                class_id=class_id,
                parent_id=level1.id,
                name=sub_name,
                chunk_count=0,
                status="auto_generated",
            )
            db.add(sub)
            db.flush()

            try:
                vector_sub = embedding_model.embed_query(f"{topic_name} - {sub_name}")
                sync_category_vector(
                    category_id=str(sub.id),
                    class_id=str(class_id),
                    name=sub_name,
                    parent_id=str(level1.id),
                    embedding=vector_sub,
                    qdrant_client=qdrant_client,
                )
            except Exception:  # noqa: BLE001
                logger.warning("Could not embed subcategory '%s'", sub_name)

        created_level1.append(level1)

    db.commit()
    for cat in created_level1:
        db.refresh(cat)

    return created_level1
