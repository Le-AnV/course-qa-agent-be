"""Integration tests for syllabus roadmap extraction and student endpoints (AI-133)."""

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.deps import get_current_user, get_db
from app.main import app
from app.models.course import Class, Enrollment
from app.models.document import Document
from app.models.user import Role, User
from app.services import document_service, ingestion_service


def _test_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    return engine


def test_ingestion_service_extracts_and_saves_syllabus_roadmap(monkeypatch, tmp_path):
    monkeypatch.setattr(ingestion_service.settings, "document_storage_dir", tmp_path)
    sample_src = Path(".vscode/data/new_syllabus/01-ai-fundamentals-and-rag_Syllabus_v1.1.xlsx")
    assert sample_src.is_file()

    dest = tmp_path / "01-ai-fundamentals-and-rag_Syllabus_v1.1.xlsx"
    dest.write_bytes(sample_src.read_bytes())

    class_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    record = SimpleNamespace(
        id=doc_id,
        class_id=class_id,
        file_name="01-ai-fundamentals-and-rag_Syllabus_v1.1.xlsx",
        version=1,
        storage_path=str(dest),
        status="processing",
    )

    db = MagicMock()
    qdrant = MagicMock()

    monkeypatch.setattr(document_service, "get_document", lambda _db, _id: record)
    monkeypatch.setattr(document_service, "build_qdrant_payload", lambda _rec: {})
    monkeypatch.setattr(document_service, "purge_document_vectors", MagicMock())

    saved_roadmap = {}

    def mock_save(_db, rec, roadmap):
        saved_roadmap["rec"] = rec
        saved_roadmap["roadmap"] = roadmap

    def mock_activate(_db, rec):
        rec.status = "indexed"

    monkeypatch.setattr(document_service, "supersede_older_versions", mock_activate)
    monkeypatch.setattr(document_service, "save_syllabus_roadmap", mock_save)
    monkeypatch.setattr(ingestion_service, "QdrantStore", lambda embeddings: qdrant)
    monkeypatch.setattr(ingestion_service, "GeminiEmbedding", lambda: object())

    ingestion_service.process_document(db, doc_id)

    assert record.status == "indexed"
    assert "roadmap" in saved_roadmap
    roadmap_data = saved_roadmap["roadmap"]
    assert roadmap_data.topic_outline is not None
    assert len(roadmap_data.units) >= 2
    assert roadmap_data.units[0].unit_number == 1
    assert "Introduction to AI" in roadmap_data.units[0].unit_title


def test_ingestion_service_fails_when_syllabus_format_unrecognized(monkeypatch, tmp_path):
    monkeypatch.setattr(ingestion_service.settings, "document_storage_dir", tmp_path)
    dest = tmp_path / "broken_syllabus.md"
    dest.write_text("Hello world without units", encoding="utf-8")

    class_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    record = SimpleNamespace(
        id=doc_id,
        class_id=class_id,
        file_name="broken_syllabus.md",
        version=1,
        storage_path=str(dest),
        status="processing",
    )

    db = MagicMock()
    monkeypatch.setattr(document_service, "get_document", lambda _db, _id: record)
    monkeypatch.setattr(document_service, "build_qdrant_payload", lambda _rec: {})
    monkeypatch.setattr(document_service, "purge_document_vectors", MagicMock())

    ingestion_service.process_document(db, doc_id)

    assert record.status == "failed"
    assert "Syllabus format is not recognized" in (record.failure_reason or "")


def test_student_syllabus_roadmap_api_and_access_control():
    engine = _test_engine()
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSession()

    r_trainer = Role(id=uuid.uuid4(), name="TRAINER")
    r_student = Role(id=uuid.uuid4(), name="STUDENT")
    db.add_all([r_trainer, r_student])
    db.commit()

    trainer = User(
        id=uuid.uuid4(),
        role_id=r_trainer.id,
        email="trainer@test.com",
        full_name="Trainer One",
    )
    student_a = User(
        id=uuid.uuid4(),
        role_id=r_student.id,
        email="student_a@test.com",
        full_name="Student A",
    )
    student_b = User(
        id=uuid.uuid4(),
        role_id=r_student.id,
        email="student_b@test.com",
        full_name="Student B",
    )
    db.add_all([trainer, student_a, student_b])
    db.commit()

    class_a = Class(
        id=uuid.uuid4(),
        trainer_id=trainer.id,
        code="CS101",
        name="Intro to CS",
        semester="FA26",
        academic_year="2026",
        status="ACTIVE",
    )
    class_b = Class(
        id=uuid.uuid4(),
        trainer_id=trainer.id,
        code="CS102",
        name="Data Structures",
        semester="FA26",
        academic_year="2026",
        status="ACTIVE",
    )
    db.add_all([class_a, class_b])
    db.commit()

    enr_a = Enrollment(
        id=uuid.uuid4(),
        class_id=class_a.id,
        student_id=student_a.id,
        status="ACTIVE",
    )
    enr_b = Enrollment(
        id=uuid.uuid4(),
        class_id=class_b.id,
        student_id=student_b.id,
        status="ACTIVE",
    )
    db.add_all([enr_a, enr_b])
    db.commit()

    # Create indexed syllabus for Class A
    doc_a = Document(
        id=uuid.uuid4(),
        class_id=class_a.id,
        uploaded_by=trainer.id,
        file_name="CS101_Syllabus.xlsx",
        file_type=".xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        status="indexed",
        version=1,
    )
    db.add(doc_a)
    db.commit()

    from app.services.ingestion.syllabus_parser import (
        ParsedSyllabusUnit,
        SyllabusRoadmapData,
    )

    roadmap_data = SyllabusRoadmapData(
        topic_outline="Unit 01: Python\nUnit 02: Advanced",
        units=[
            ParsedSyllabusUnit(
                unit_number=1,
                unit_title="Python Basics",
                description="2 sessions",
                content="### Session 1\n- Intro",
            ),
            ParsedSyllabusUnit(
                unit_number=2,
                unit_title="Advanced OOP",
                description="2 sessions",
                content="### Session 2\n- OOP",
            ),
        ],
    )
    document_service.save_syllabus_roadmap(db, doc_a, roadmap_data)

    current_caller = None

    def override_get_db():
        try:
            yield db
        finally:
            pass

    def override_current_user():
        return current_caller

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    client = TestClient(app)

    try:
        # Student A reads Class A syllabus -> OK
        current_caller = student_a
        res = client.get(f"/api/student/classes/{class_a.id}/syllabus")
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["class_id"] == str(class_a.id)
        assert body["document_id"] == str(doc_a.id)
        assert body["file_name"] == "CS101_Syllabus.xlsx"
        assert "Unit 01: Python" in body["topic_outline"]
        assert len(body["units"]) == 2
        assert body["units"][0]["unit_number"] == 1
        assert body["units"][0]["unit_title"] == "Python Basics"
        assert body["units"][1]["unit_number"] == 2

        # Student B reads Class A syllabus -> 403 Forbidden (AC-06: no leak across classes)
        current_caller = student_b
        res_forbidden = client.get(f"/api/student/classes/{class_a.id}/syllabus")
        assert res_forbidden.status_code == 403

        # Trainer reads Class A syllabus -> OK
        current_caller = trainer
        res_trainer = client.get(f"/api/student/classes/{class_a.id}/syllabus")
        assert res_trainer.status_code == 200

        # Class B has no syllabus yet -> returns empty units with 200
        current_caller = student_b
        res_empty = client.get(f"/api/student/classes/{class_b.id}/syllabus")
        assert res_empty.status_code == 200
        assert res_empty.json()["units"] == []
        assert res_empty.json()["document_id"] is None
    finally:
        app.dependency_overrides.clear()
        db.close()


def test_reupload_syllabus_replaces_previous_roadmap_cleanly():
    engine = _test_engine()
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    db = TestingSession()

    class_id = uuid.uuid4()
    trainer_id = uuid.uuid4()

    doc_v1 = Document(
        id=uuid.uuid4(),
        class_id=class_id,
        uploaded_by=trainer_id,
        file_name="syllabus.xlsx",
        file_type=".xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        status="indexed",
        version=1,
    )
    db.add(doc_v1)
    db.commit()

    from app.services.ingestion.syllabus_parser import (
        ParsedSyllabusUnit,
        SyllabusRoadmapData,
    )

    roadmap_v1 = SyllabusRoadmapData(
        topic_outline="Old outline",
        units=[
            ParsedSyllabusUnit(
                unit_number=1,
                unit_title="Old Unit 1",
                description=None,
                content="Old content",
            )
        ],
    )
    document_service.save_syllabus_roadmap(db, doc_v1, roadmap_v1)

    _, outline, units = document_service.get_class_syllabus_roadmap(db, class_id)
    assert len(units) == 1
    assert units[0].unit_title == "Old Unit 1"

    # Re-upload: new version v2
    doc_v2 = Document(
        id=uuid.uuid4(),
        class_id=class_id,
        uploaded_by=trainer_id,
        file_name="syllabus.xlsx",
        file_type=".xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        status="indexed",
        version=2,
    )
    db.add(doc_v2)
    db.commit()

    roadmap_v2 = SyllabusRoadmapData(
        topic_outline="New outline",
        units=[
            ParsedSyllabusUnit(
                unit_number=1,
                unit_title="New Unit 1",
                description="Fresh",
                content="New content 1",
            ),
            ParsedSyllabusUnit(
                unit_number=2,
                unit_title="New Unit 2",
                description="Fresh",
                content="New content 2",
            ),
        ],
    )
    document_service.save_syllabus_roadmap(db, doc_v2, roadmap_v2)

    doc, outline, units = document_service.get_class_syllabus_roadmap(db, class_id)
    assert doc.id == doc_v2.id
    assert outline == "New outline"
    assert len(units) == 2
    assert units[0].unit_title == "New Unit 1"
    assert units[1].unit_title == "New Unit 2"

    db.close()
