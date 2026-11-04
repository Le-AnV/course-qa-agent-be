"""Phase 2 unit tests for Category Management and Vector Taxonomy (AI-91 AC-04 to AC-08).

Covers:
- Category Qdrant collection creation ('class_categories')
- 2-level hierarchy enforcement (Level 1: Topic, Level 2: Subtopic, reject Level 3)
- Category creation, listing, updating, and cascade deletion
- Category embedding synchronization in Qdrant
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest
from qdrant_client.http import models as qmodels
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.models.category import ClassCategory
from app.models.course import Class
from app.models.user import User
from app.schemas.category import CategoryCreate, CategoryUpdate
from app.services import category_service


@pytest.fixture()
def db():
    """Isolated in-memory SQLite session for category tests."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = SessionLocal()

    from app.models.user import Role

    role = Role(name="TRAINER", description="Trainer role")
    session.add(role)
    session.flush()

    trainer = User(
        id=uuid.uuid4(),
        role_id=role.id,
        email="teacher@academic.edu.vn",
        full_name="Test Trainer",
    )
    session.add(trainer)
    session.flush()

    test_class = Class(
        id=uuid.uuid4(),
        trainer_id=trainer.id,
        code="CS101",
        name="Data Structures & Algorithms",
        semester="1",
        academic_year="2026-2027",
    )
    session.add(test_class)
    session.commit()

    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_ensure_category_collection():
    mock_client = MagicMock()
    mock_client.collection_exists.return_value = False

    category_service.ensure_category_collection(mock_client)

    mock_client.collection_exists.assert_called_once_with(category_service.CATEGORY_COLLECTION)
    mock_client.create_collection.assert_called_once()
    _, kwargs = mock_client.create_collection.call_args
    assert kwargs["collection_name"] == category_service.CATEGORY_COLLECTION
    assert kwargs["vectors_config"].size == 1536
    assert kwargs["vectors_config"].distance == qmodels.Distance.COSINE


def test_create_and_list_2_level_categories(db):
    test_class = db.query(Class).first()
    class_id = test_class.id
    mock_qdrant = MagicMock()

    with patch("app.services.category_service.GeminiEmbedding") as mock_emb_cls:
        mock_emb = MagicMock()
        mock_emb.embed_query.return_value = [0.1] * 1536
        mock_emb_cls.return_value = mock_emb

        # 1. Create Level 1 topic
        cat1 = category_service.create_category(
            db=db,
            class_id=class_id,
            payload=CategoryCreate(name="Tree Structures", parent_id=None),
            qdrant_client=mock_qdrant,
        )
        assert cat1.name == "Tree Structures"
        assert cat1.parent_id is None

        # 2. Create Level 2 subtopics
        sub1 = category_service.create_category(
            db=db,
            class_id=class_id,
            payload=CategoryCreate(name="Binary Search Tree", parent_id=cat1.id),
            qdrant_client=mock_qdrant,
        )
        sub2 = category_service.create_category(
            db=db,
            class_id=class_id,
            payload=CategoryCreate(name="AVL Trees", parent_id=cat1.id),
            qdrant_client=mock_qdrant,
        )

        assert sub1.parent_id == cat1.id
        assert sub2.parent_id == cat1.id

        # 3. Listing returns Level 1 with nested Level 2
        tree = category_service.list_categories(db, class_id)
        assert tree.total_categories == 3
        assert len(tree.items) == 1

        parent_item = tree.items[0]
        assert parent_item.name == "Tree Structures"
        assert len(parent_item.subcategories) == 2
        sub_names = [s.name for s in parent_item.subcategories]
        assert "AVL Trees" in sub_names
        assert "Binary Search Tree" in sub_names


def test_reject_level_3_nesting(db):
    """AC-04: Enforce maximum exactly 2 levels: Topic -> Subtopics."""
    test_class = db.query(Class).first()
    class_id = test_class.id

    with patch("app.services.category_service.GeminiEmbedding") as mock_emb_cls:
        mock_emb = MagicMock()
        mock_emb.embed_query.return_value = [0.1] * 1536
        mock_emb_cls.return_value = mock_emb

        # Level 1
        level1 = category_service.create_category(
            db=db,
            class_id=class_id,
            payload=CategoryCreate(name="Graph Algorithms", parent_id=None),
            qdrant_client=MagicMock(),
        )

        # Level 2
        level2 = category_service.create_category(
            db=db,
            class_id=class_id,
            payload=CategoryCreate(name="Shortest Path", parent_id=level1.id),
            qdrant_client=MagicMock(),
        )

        # Level 3 attempt should fail
        with pytest.raises(ValueError, match="maximum 2 levels"):
            category_service.create_category(
                db=db,
                class_id=class_id,
                payload=CategoryCreate(name="Dijkstra Algorithm", parent_id=level2.id),
                qdrant_client=MagicMock(),
            )


def test_update_and_delete_category(db):
    test_class = db.query(Class).first()
    class_id = test_class.id
    mock_qdrant = MagicMock()

    with patch("app.services.category_service.GeminiEmbedding") as mock_emb_cls:
        mock_emb = MagicMock()
        mock_emb.embed_query.return_value = [0.1] * 1536
        mock_emb_cls.return_value = mock_emb

        cat = category_service.create_category(
            db=db,
            class_id=class_id,
            payload=CategoryCreate(name="Sorting", parent_id=None),
            qdrant_client=mock_qdrant,
        )

        # Update
        updated = category_service.update_category(
            db=db,
            class_id=class_id,
            category_id=cat.id,
            payload=CategoryUpdate(name="Advanced Sorting Algorithms"),
            qdrant_client=mock_qdrant,
        )
        assert updated.name == "Advanced Sorting Algorithms"

        # Delete
        success = category_service.delete_category(
            db=db,
            class_id=class_id,
            category_id=cat.id,
            qdrant_client=mock_qdrant,
        )
        assert success is True
        assert db.get(ClassCategory, cat.id) is None
        mock_qdrant.delete.assert_called()
