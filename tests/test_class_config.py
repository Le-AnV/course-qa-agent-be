"""Class configuration: capacity (P5), typed config (P6), archive (P7).

Covers the API contract:
- GET   /api/classes/{id}/config          -> effective defaults without a row
- PATCH /api/classes/{id}/config          -> partial update, constrained values
- POST  /api/classes/{id}/config/copy     -> template copy (capacity excluded)
- PATCH /api/admin/classes/{id}/status    -> ARCHIVED hides the class everywhere
- capacity is a hard gate on enrollment approval
"""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.main import app

TEST_DB_URL = "sqlite://"


@pytest.fixture()
def db_session(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.core.database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", TEST_DB_URL)

    engine = create_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    dbmod._db_url = TEST_DB_URL
    Base.metadata.create_all(bind=engine)
    SessionLocal = get_session_local(TEST_DB_URL)
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
    reset_engine()


@pytest.fixture()
def api_client(db_session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def admin_headers(client: TestClient) -> dict:
    res = client.post(
        "/api/auth/login",
        json={
            "email": settings.bootstrap_admin_email,
            "password": settings.bootstrap_admin_password,
        },
    )
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def make_user(client: TestClient, admin: dict, email: str, name: str, role: str) -> str:
    res = client.post(
        "/api/admin/users",
        json={"username": name, "email": email, "role": role},
        headers=admin,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def token_for(db_session, email: str, role: str) -> dict:
    from app.core.security import create_access_token
    from app.models.user import User

    user = db_session.query(User).filter(User.email == email).first()
    return {"Authorization": f"Bearer {create_access_token(str(user.id), role)}"}


def make_class(client: TestClient, admin: dict, code: str, trainer_id: str) -> str:
    res = client.post(
        "/api/admin/classes",
        json={"code": code, "trainer_id": trainer_id, "credits": 3},
        headers=admin,
    )
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_config_defaults_when_nothing_is_saved(api_client):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", trainer)

    res = api_client.get(f"/api/classes/{class_id}/config", headers=admin)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["capacity"] is None
    assert body["delivery_mode"] == "on_campus"
    assert body["default_language"] == "en"
    assert body["answer_scope"] == "syllabus_first"
    assert body["citation_required"] is True
    assert body["max_answer_words"] == 250
    assert body["session_length_minutes"] == 90


def test_partial_update_keeps_untouched_fields(api_client):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", trainer)

    res = api_client.patch(
        f"/api/classes/{class_id}/config",
        json={"capacity": 40, "delivery_mode": "hybrid"},
        headers=admin,
    )
    assert res.status_code == 200, res.text
    assert res.json()["capacity"] == 40
    assert res.json()["delivery_mode"] == "hybrid"
    # Untouched fields keep their defaults.
    assert res.json()["max_answer_words"] == 250

    # Omitted keys never reset values; capacity can also be cleared explicitly.
    cleared = api_client.patch(
        f"/api/classes/{class_id}/config", json={"capacity": None}, headers=admin
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["capacity"] is None
    assert cleared.json()["delivery_mode"] == "hybrid"


def test_constrained_fields_reject_unknown_values(api_client):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", trainer)

    # The closed vocabularies are enforced at the schema edge (422).
    bad_mode = api_client.patch(
        f"/api/classes/{class_id}/config",
        json={"delivery_mode": "telepathy"},
        headers=admin,
    )
    assert bad_mode.status_code == 422

    bad_words = api_client.patch(
        f"/api/classes/{class_id}/config",
        json={"max_answer_words": 5000},
        headers=admin,
    )
    assert bad_words.status_code == 422

    # Nothing was persisted by the rejected requests.
    current = api_client.get(f"/api/classes/{class_id}/config", headers=admin).json()
    assert current["delivery_mode"] == "on_campus"
    assert current["max_answer_words"] == 250


def test_copy_config_copies_teaching_fields_but_not_capacity(api_client):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    source = make_class(api_client, admin, "CS101-A", trainer)
    target = make_class(api_client, admin, "CS101-B", trainer)

    api_client.patch(
        f"/api/classes/{source}/config",
        json={
            "capacity": 12,
            "delivery_mode": "online",
            "default_language": "vi",
            "citation_required": False,
            "max_answer_words": 400,
        },
        headers=admin,
    )
    api_client.patch(f"/api/classes/{target}/config", json={"capacity": 99}, headers=admin)

    res = api_client.post(
        f"/api/classes/{target}/config/copy",
        json={"source_class_id": source},
        headers=admin,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["delivery_mode"] == "online"
    assert body["default_language"] == "vi"
    assert body["citation_required"] is False
    assert body["max_answer_words"] == 400
    # Capacity is per-class, so it survives the copy untouched.
    assert body["capacity"] == 99

    # A class cannot copy from itself.
    same = api_client.post(
        f"/api/classes/{target}/config/copy",
        json={"source_class_id": target},
        headers=admin,
    )
    assert same.status_code == 400


def test_trainer_outside_the_class_cannot_read_or_change_config(api_client, db_session):
    admin = admin_headers(api_client)
    first = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    make_user(api_client, admin, "t2@gmail.com", "Trainer Two", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", first)

    outsider = token_for(db_session, "t2@gmail.com", "trainer")
    assert api_client.get(f"/api/classes/{class_id}/config", headers=outsider).status_code == 403

    owner = token_for(db_session, "t1@gmail.com", "trainer")
    ok = api_client.patch(
        f"/api/classes/{class_id}/config", json={"delivery_mode": "hybrid"}, headers=owner
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["delivery_mode"] == "hybrid"


def test_only_admin_can_change_capacity(api_client, db_session):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", trainer)
    owner = token_for(db_session, "t1@gmail.com", "trainer")

    denied = api_client.patch(
        f"/api/classes/{class_id}/config", json={"capacity": 30}, headers=owner
    )
    assert denied.status_code == 403, denied.text
    assert "admin" in denied.json()["detail"].lower()

    # Clearing the limit is an admin decision as well.
    clear_denied = api_client.patch(
        f"/api/classes/{class_id}/config", json={"capacity": None}, headers=owner
    )
    assert clear_denied.status_code == 403, clear_denied.text

    # A patch that mixes capacity with teaching fields is rejected whole.
    mixed = api_client.patch(
        f"/api/classes/{class_id}/config",
        json={"capacity": 40, "delivery_mode": "online"},
        headers=owner,
    )
    assert mixed.status_code == 403, mixed.text
    assert api_client.get(f"/api/classes/{class_id}/config", headers=owner).json()[
        "delivery_mode"
    ] == "on_campus"

    # Admins keep full control: set and clear.
    set_by_admin = api_client.patch(
        f"/api/classes/{class_id}/config", json={"capacity": 30}, headers=admin
    )
    assert set_by_admin.status_code == 200, set_by_admin.text
    assert set_by_admin.json()["capacity"] == 30
    clear_by_admin = api_client.patch(
        f"/api/classes/{class_id}/config", json={"capacity": None}, headers=admin
    )
    assert clear_by_admin.status_code == 200, clear_by_admin.text
    assert clear_by_admin.json()["capacity"] is None


def student_auth(user_id: str) -> dict:
    from app.core.security import create_access_token

    return {"Authorization": f"Bearer {create_access_token(user_id, 'student')}"}


def join_and_approve(client: TestClient, admin: dict, class_id: str, student_id: str):
    client.post(f"/api/student/classes/{class_id}/request", headers=student_auth(student_id))
    return client.post(
        f"/api/student/classes/{class_id}/requests/{student_id}/approve", headers=admin
    )


def test_capacity_cannot_drop_below_the_current_roster(api_client):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    one = make_user(api_client, admin, "s1@gmail.com", "Student One", "student")
    two = make_user(api_client, admin, "s2@gmail.com", "Student Two", "student")
    class_id = make_class(api_client, admin, "CS101-A", trainer)
    api_client.patch(f"/api/classes/{class_id}/config", json={"capacity": 5}, headers=admin)

    assert join_and_approve(api_client, admin, class_id, one).status_code == 200
    assert join_and_approve(api_client, admin, class_id, two).status_code == 200

    below = api_client.patch(
        f"/api/classes/{class_id}/config", json={"capacity": 1}, headers=admin
    )
    assert below.status_code == 400, below.text
    assert "already enrolled" in below.json()["detail"].lower()

    # Exactly the roster size is still fine.
    exact = api_client.patch(
        f"/api/classes/{class_id}/config", json={"capacity": 2}, headers=admin
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["capacity"] == 2


def test_full_class_blocks_new_approvals(api_client):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    one = make_user(api_client, admin, "s1@gmail.com", "Student One", "student")
    two = make_user(api_client, admin, "s2@gmail.com", "Student Two", "student")
    class_id = make_class(api_client, admin, "CS101-A", trainer)
    api_client.patch(f"/api/classes/{class_id}/config", json={"capacity": 1}, headers=admin)

    api_client.post(f"/api/student/classes/{class_id}/request", headers=student_auth(one))
    first = api_client.post(
        f"/api/student/classes/{class_id}/requests/{one}/approve", headers=admin
    )
    assert first.status_code == 200, first.text

    api_client.post(f"/api/student/classes/{class_id}/request", headers=student_auth(two))
    second = api_client.post(
        f"/api/student/classes/{class_id}/requests/{two}/approve", headers=admin
    )
    assert second.status_code == 400, second.text
    assert "full" in second.json()["detail"].lower()


def test_archived_class_leaves_the_workspace_and_can_be_restored(api_client, db_session):
    admin = admin_headers(api_client)
    trainer_id = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", trainer_id)
    trainer = token_for(db_session, "t1@gmail.com", "trainer")

    assert [c["id"] for c in api_client.get("/api/student/classes", headers=trainer).json()] == [
        class_id
    ]

    archived = api_client.patch(
        f"/api/admin/classes/{class_id}/status",
        json={"status": "ARCHIVED"},
        headers=admin,
    )
    assert archived.status_code == 200, archived.text
    assert archived.json()["status"] == "ARCHIVED"

    # Hidden from the trainer's workspace and unreachable by id.
    assert api_client.get("/api/student/classes", headers=trainer).json() == []
    assert api_client.get(f"/api/student/classes/{class_id}", headers=trainer).status_code == 404
    assert api_client.get("/api/documents/classes/mine", headers=trainer).json() == []
    # Admins still see it (they need it to restore) and can list it.
    listed = api_client.get("/api/admin/classes", headers=admin).json()
    assert [c["status"] for c in listed if c["id"] == class_id] == ["ARCHIVED"]

    restored = api_client.patch(
        f"/api/admin/classes/{class_id}/status",
        json={"status": "ACTIVE"},
        headers=admin,
    )
    assert restored.status_code == 200, restored.text
    assert [c["id"] for c in api_client.get("/api/student/classes", headers=trainer).json()] == [
        class_id
    ]


def test_admin_sees_capacity_and_status_on_the_class_list(api_client):
    admin = admin_headers(api_client)
    trainer = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    class_id = make_class(api_client, admin, "CS101-A", trainer)
    api_client.patch(f"/api/classes/{class_id}/config", json={"capacity": 42}, headers=admin)

    listed = api_client.get("/api/admin/classes", headers=admin).json()
    row = next(c for c in listed if c["id"] == class_id)
    assert row["capacity"] == 42
    assert row["status"] == "ACTIVE"


def test_copy_sources_list_only_the_callers_own_classes(api_client, db_session):
    admin = admin_headers(api_client)
    first = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    second = make_user(api_client, admin, "t2@gmail.com", "Trainer Two", "trainer")
    mine = make_class(api_client, admin, "CS101-A", first)
    theirs = make_class(api_client, admin, "CS101-B", second)

    owner = token_for(db_session, "t1@gmail.com", "trainer")
    sources = api_client.get("/api/classes/config-sources", headers=owner).json()
    assert [s["id"] for s in sources] == [mine]

    # An admin may reuse any class as a template.
    as_admin = api_client.get("/api/classes/config-sources", headers=admin).json()
    assert {s["id"] for s in as_admin} >= {mine, theirs}


def test_trainer_cannot_copy_config_from_another_trainers_class(api_client, db_session):
    admin = admin_headers(api_client)
    first = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    second = make_user(api_client, admin, "t2@gmail.com", "Trainer Two", "trainer")
    mine = make_class(api_client, admin, "CS101-A", first)
    theirs = make_class(api_client, admin, "CS101-B", second)
    api_client.patch(
        f"/api/classes/{theirs}/config",
        json={"delivery_mode": "online", "session_length_minutes": 150},
        headers=admin,
    )

    owner = token_for(db_session, "t1@gmail.com", "trainer")
    blocked = api_client.post(
        f"/api/classes/{mine}/config/copy",
        json={"source_class_id": theirs},
        headers=owner,
    )
    assert blocked.status_code == 400, blocked.text
    assert "class you teach" in blocked.json()["detail"]
    assert api_client.get(f"/api/classes/{mine}/config", headers=owner).json()[
        "delivery_mode"
    ] == "on_campus"

    # Copying from your own class still works.
    other = make_class(api_client, admin, "CS101-A2", first)
    ok = api_client.post(
        f"/api/classes/{other}/config/copy",
        json={"source_class_id": mine},
        headers=owner,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["capacity"] is None


def test_archived_class_still_works_as_a_template_source(api_client, db_session):
    admin = admin_headers(api_client)
    trainer_id = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    source = make_class(api_client, admin, "CS101-OLD", trainer_id)
    target = make_class(api_client, admin, "CS101-NEW", trainer_id)
    api_client.patch(
        f"/api/classes/{source}/config",
        json={"delivery_mode": "online", "session_length_minutes": 150},
        headers=admin,
    )
    api_client.patch(f"/api/classes/{source}/info", json={"note": "Old term."}, headers=admin)
    api_client.patch(
        f"/api/admin/classes/{source}/status", json={"status": "ARCHIVED"}, headers=admin
    )

    owner = token_for(db_session, "t1@gmail.com", "trainer")
    sources = api_client.get("/api/classes/config-sources", headers=owner).json()
    archived = [s for s in sources if s["id"] == source]
    assert archived and archived[0]["status"] == "ARCHIVED"

    copied = api_client.post(
        f"/api/classes/{target}/template-copy",
        json={"source_class_id": source, "document_ids": [], "info_fields": ["note"]},
        headers=owner,
    )
    assert copied.status_code == 200, copied.text
    body = copied.json()
    assert body["config"]["delivery_mode"] == "online"
    assert body["config"]["session_length_minutes"] == 150
    assert body["info"]["note"] == "Old term."


def test_template_copy_only_changes_selected_info_fields(api_client):
    admin = admin_headers(api_client)
    trainer_id = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    source = make_class(api_client, admin, "CS101-OLD", trainer_id)
    target = make_class(api_client, admin, "CS101-NEW", trainer_id)
    api_client.patch(
        f"/api/classes/{source}/info",
        json={"description": "Source description", "note": "Source note"},
        headers=admin,
    )
    api_client.patch(
        f"/api/classes/{target}/info",
        json={"description": "Target description", "note": "Target note"},
        headers=admin,
    )

    copied = api_client.post(
        f"/api/classes/{target}/template-copy",
        json={
            "source_class_id": source,
            "document_ids": [],
            "info_fields": ["description"],
        },
        headers=admin,
    )

    assert copied.status_code == 200, copied.text
    info = copied.json()["info"]
    assert info["description"] == "Source description"
    assert info["note"] == "Target note"


def test_template_copy_does_not_clear_target_info_when_source_field_is_empty(api_client):
    admin = admin_headers(api_client)
    trainer_id = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    source = make_class(api_client, admin, "CS101-OLD", trainer_id)
    target = make_class(api_client, admin, "CS101-NEW", trainer_id)
    api_client.patch(
        f"/api/classes/{target}/info",
        json={"description": "Keep this", "meeting_url": "https://meet.example.com/keep"},
        headers=admin,
    )

    copied = api_client.post(
        f"/api/classes/{target}/template-copy",
        json={
            "source_class_id": source,
            "document_ids": [],
            "info_fields": ["description", "meeting_url"],
        },
        headers=admin,
    )

    assert copied.status_code == 200, copied.text
    info = copied.json()["info"]
    assert info["description"] == "Keep this"
    assert info["meeting_url"] == "https://meet.example.com/keep"


def test_template_copy_can_skip_teaching_config(api_client):
    admin = admin_headers(api_client)
    trainer_id = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    source = make_class(api_client, admin, "CS101-OLD", trainer_id)
    target = make_class(api_client, admin, "CS101-NEW", trainer_id)
    api_client.patch(
        f"/api/classes/{source}/config",
        json={"delivery_mode": "online", "session_length_minutes": 150},
        headers=admin,
    )

    copied = api_client.post(
        f"/api/classes/{target}/template-copy",
        json={
            "source_class_id": source,
            "document_ids": [],
            "copy_config": False,
        },
        headers=admin,
    )

    assert copied.status_code == 200, copied.text
    config = copied.json()["config"]
    assert config["delivery_mode"] == "on_campus"
    assert config["session_length_minutes"] == 90


def test_template_copy_rejects_unknown_info_field(api_client):
    admin = admin_headers(api_client)
    trainer_id = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    source = make_class(api_client, admin, "CS101-OLD", trainer_id)
    target = make_class(api_client, admin, "CS101-NEW", trainer_id)

    copied = api_client.post(
        f"/api/classes/{target}/template-copy",
        json={
            "source_class_id": source,
            "document_ids": [],
            "info_fields": ["bogus"],
        },
        headers=admin,
    )

    assert copied.status_code == 400
    assert "info" in copied.json()["detail"].lower()


def _make_document(db_session, class_id, uploader_id, tmp_path, name, status="indexed", version=1):
    import uuid
    from pathlib import Path

    from app.models.document import Document

    path = Path(tmp_path) / f"{uuid.uuid4()}{Path(name).suffix}"
    content = f"content of {name}".encode()
    path.write_bytes(content)
    row = Document(
        class_id=uuid.UUID(class_id),
        uploaded_by=uuid.UUID(uploader_id),
        file_name=name,
        file_type=Path(name).suffix,
        mime_type="application/pdf",
        size_bytes=len(content),
        status=status,
        version=version,
        storage_path=str(path),
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def test_template_documents_lists_only_current_indexed_and_clones_selected(
    api_client, db_session, tmp_path, monkeypatch
):
    from app.config import settings
    from app.models.document import Document
    from app.services import document_service

    monkeypatch.setattr(settings, "document_storage_dir", tmp_path)
    monkeypatch.setattr(
        document_service, "clone_document_points", lambda *args, **kwargs: 3
    )

    admin = admin_headers(api_client)
    trainer_id = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    source = make_class(api_client, admin, "CS101-OLD", trainer_id)
    target = make_class(api_client, admin, "CS101-NEW", trainer_id)
    owner = token_for(db_session, "t1@gmail.com", "trainer")

    current = _make_document(db_session, source, trainer_id, tmp_path, "lesson.pdf")
    replaced = _make_document(
        db_session, source, trainer_id, tmp_path, "old.pdf", status="replaced", version=1
    )
    newer = _make_document(
        db_session, source, trainer_id, tmp_path, "lesson.pdf", status="indexed", version=2
    )
    _make_document(db_session, source, trainer_id, tmp_path, "draft.pdf", status="pending")

    listed = api_client.get(
        f"/api/classes/{target}/template-documents",
        params={"source_class_id": source},
        headers=owner,
    )
    assert listed.status_code == 200, listed.text
    assert {d["id"] for d in listed.json()} == {str(newer.id)}

    copied = api_client.post(
        f"/api/classes/{target}/template-copy",
        json={"source_class_id": source, "document_ids": [str(newer.id), str(replaced.id)]},
        headers=owner,
    )
    assert copied.status_code == 200, copied.text
    body = copied.json()
    assert [d["file_name"] for d in body["copied_documents"]] == ["lesson.pdf"]
    assert body["copied_documents"][0]["version"] == 1
    assert len(body["skipped_documents"]) == 1

    rows = db_session.query(Document).filter(Document.class_id == uuid.UUID(target)).all()
    assert len(rows) == 1
    clone = rows[0]
    assert clone.status == "indexed"
    assert clone.version == 1
    assert clone.storage_path != newer.storage_path
    assert Path(clone.storage_path).read_bytes() == Path(newer.storage_path).read_bytes()
    # The source class is untouched.
    assert db_session.get(Document, newer.id).class_id == uuid.UUID(source)
    assert db_session.get(Document, current.id).status == "indexed"


def test_template_copy_skips_duplicate_names_and_outsiders_cannot_preview(
    api_client, db_session, tmp_path, monkeypatch
):
    from app.config import settings
    from app.services import document_service

    monkeypatch.setattr(settings, "document_storage_dir", tmp_path)
    monkeypatch.setattr(
        document_service, "clone_document_points", lambda *args, **kwargs: 1
    )

    admin = admin_headers(api_client)
    first = make_user(api_client, admin, "t1@gmail.com", "Trainer One", "trainer")
    make_user(api_client, admin, "t2@gmail.com", "Trainer Two", "trainer")
    source = make_class(api_client, admin, "CS101-A", first)
    target = make_class(api_client, admin, "CS101-B", first)
    owner = token_for(db_session, "t1@gmail.com", "trainer")
    outsider = token_for(db_session, "t2@gmail.com", "trainer")

    source_doc = _make_document(db_session, source, first, tmp_path, "lesson.pdf")
    _make_document(db_session, target, first, tmp_path, "lesson.pdf")

    denied = api_client.get(
        f"/api/classes/{target}/template-documents",
        params={"source_class_id": source},
        headers=outsider,
    )
    assert denied.status_code == 403

    copied = api_client.post(
        f"/api/classes/{target}/template-copy",
        json={"source_class_id": source, "document_ids": [str(source_doc.id)]},
        headers=owner,
    )
    assert copied.status_code == 200, copied.text
    body = copied.json()
    assert body["copied_documents"] == []
    assert body["skipped_documents"][0]["file_name"] == "lesson.pdf"
