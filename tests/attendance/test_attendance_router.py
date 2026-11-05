"""Integration tests for attendance routers (AI-136 Phase 1, Step 1.6).

Covers Endpoints 1–5:
1. GET /api/classes/{class_id}/schedules (401, 403, 200)
2. POST /api/classes/{class_id}/schedules (401, 403, 409, 422, 201)
3. DELETE /api/classes/{class_id}/schedules/{schedule_id} (401, 403, 404, 200)
4. GET /api/classes/{class_id}/sessions?from=&to= (401, 403, 400, 200, lazy generation & idempotency)
5. POST /api/classes/{class_id}/sessions (401, 403, 409 holiday, 409 duplicate slot, 201)
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import models  # noqa: F401
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.security import create_access_token
from app.main import app
from app.models.attendance import ClassSession
from app.models.course import Class, ClassTrainer, Enrollment
from app.models.user import Role, User
from app.services.holiday_service import VIETNAM_TZ


@pytest.fixture()
def db_session(monkeypatch) -> Generator[Session, None, None]:
    """In-memory SQLite database populated with users and a test class."""
    monkeypatch.setattr("app.services.reranker_service.warm_up", lambda: None)

    import app.core.database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", "sqlite://")
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dbmod._engine = engine
    dbmod._SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    dbmod._db_url = "sqlite://"
    Base.metadata.create_all(bind=engine)

    db = get_session_local("sqlite://")()

    # Seed roles
    admin_role = Role(name="ADMIN")
    trainer_role = Role(name="TRAINER")
    student_role = Role(name="STUDENT")
    db.add_all([admin_role, trainer_role, student_role])
    db.flush()

    # Seed users
    admin_user = User(
        full_name="Admin User",
        email="admin@academic.edu.vn",
        role_id=admin_role.id,
        status="ACTIVE",
    )
    primary_trainer = User(
        full_name="Primary Trainer",
        email="trainer1@academic.edu.vn",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    co_trainer = User(
        full_name="Co Trainer",
        email="cotrainer@academic.edu.vn",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    other_trainer = User(
        full_name="Other Trainer",
        email="other_trainer@academic.edu.vn",
        role_id=trainer_role.id,
        status="ACTIVE",
    )
    enrolled_student = User(
        full_name="Enrolled Student",
        email="student1@academic.edu.vn",
        role_id=student_role.id,
        status="ACTIVE",
    )
    other_student = User(
        full_name="Other Student",
        email="student2@academic.edu.vn",
        role_id=student_role.id,
        status="ACTIVE",
    )
    db.add_all(
        [
            admin_user,
            primary_trainer,
            co_trainer,
            other_trainer,
            enrolled_student,
            other_student,
        ]
    )
    db.flush()

    # Seed class
    clazz = Class(
        code="CS101-FA26-001",
        name="Computer Science 101",
        trainer_id=primary_trainer.id,
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
        credits=3,
    )
    db.add(clazz)
    db.flush()

    # Add co-trainer and enrolled student
    db.add(ClassTrainer(class_id=clazz.id, trainer_id=co_trainer.id))
    db.add(Enrollment(class_id=clazz.id, student_id=enrolled_student.id, status="ACTIVE"))
    db.commit()

    db.info.update(
        admin_id=admin_user.id,
        primary_trainer_id=primary_trainer.id,
        co_trainer_id=co_trainer.id,
        other_trainer_id=other_trainer.id,
        enrolled_student_id=enrolled_student.id,
        other_student_id=other_student.id,
        class_id=clazz.id,
    )
    try:
        yield db
    finally:
        db.close()
        engine.dispose()
        reset_engine()


@pytest.fixture()
def client(db_session: Session) -> Generator[TestClient, None, None]:
    """TestClient configured with SQLite database dependency override."""
    def _override_db():
        yield db_session

    app.dependency_overrides[get_db] = _override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _headers(user_id: uuid.UUID, role: str = "trainer") -> dict[str, str]:
    token = create_access_token(str(user_id), role)
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# 1. Authentication tests (401)
# ---------------------------------------------------------------------------


def test_endpoints_require_authentication(client: TestClient, db_session: Session):
    class_id = db_session.info["class_id"]

    # Schedules
    assert client.get(f"/api/classes/{class_id}/schedules").status_code == 401
    assert client.post(f"/api/classes/{class_id}/schedules", json={}).status_code == 401
    assert (
        client.delete(f"/api/classes/{class_id}/schedules/{uuid.uuid4()}").status_code == 401
    )

    # Sessions
    assert client.get(f"/api/classes/{class_id}/sessions").status_code == 401
    assert client.post(f"/api/classes/{class_id}/sessions", json={}).status_code == 401

    # Attendance Sheet & Summary
    fake_session_id = uuid.uuid4()
    assert (
        client.get(f"/api/classes/{class_id}/sessions/{fake_session_id}/attendance").status_code
        == 401
    )
    assert (
        client.put(
            f"/api/classes/{class_id}/sessions/{fake_session_id}/attendance", json={}
        ).status_code
        == 401
    )
    assert client.get(f"/api/classes/{class_id}/attendance/summary").status_code == 401

    # Student Attendance History
    assert client.get(f"/api/student/classes/{class_id}/attendance").status_code == 401


# ---------------------------------------------------------------------------
# 2. Permissions tests (D14)
# ---------------------------------------------------------------------------


def test_schedule_endpoints_permissions(client: TestClient, db_session: Session):
    class_id = db_session.info["class_id"]
    primary_hdr = _headers(db_session.info["primary_trainer_id"], "trainer")
    co_hdr = _headers(db_session.info["co_trainer_id"], "trainer")
    other_trainer_hdr = _headers(db_session.info["other_trainer_id"], "trainer")
    student_hdr = _headers(db_session.info["enrolled_student_id"], "student")
    admin_hdr = _headers(db_session.info["admin_id"], "admin")

    valid_payload = {
        "weekday": 0,
        "start_time": "19:00",
        "end_time": "21:00",
        "effective_from": "2026-09-01",
    }

    # Student cannot read or write schedules (403)
    assert client.get(f"/api/classes/{class_id}/schedules", headers=student_hdr).status_code == 403
    assert (
        client.post(f"/api/classes/{class_id}/schedules", json=valid_payload, headers=student_hdr).status_code
        == 403
    )

    # Outsider trainer cannot read or write schedules (403)
    assert client.get(f"/api/classes/{class_id}/schedules", headers=other_trainer_hdr).status_code == 403
    assert (
        client.post(
            f"/api/classes/{class_id}/schedules", json=valid_payload, headers=other_trainer_hdr
        ).status_code
        == 403
    )

    # Primary trainer, Co-trainer, Admin can access (200)
    assert client.get(f"/api/classes/{class_id}/schedules", headers=primary_hdr).status_code == 200
    assert client.get(f"/api/classes/{class_id}/schedules", headers=co_hdr).status_code == 200
    assert client.get(f"/api/classes/{class_id}/schedules", headers=admin_hdr).status_code == 200


def test_sessions_endpoint_permissions(client: TestClient, db_session: Session):
    class_id = db_session.info["class_id"]
    enrolled_student_hdr = _headers(db_session.info["enrolled_student_id"], "student")
    other_student_hdr = _headers(db_session.info["other_student_id"], "student")
    other_trainer_hdr = _headers(db_session.info["other_trainer_id"], "trainer")
    primary_hdr = _headers(db_session.info["primary_trainer_id"], "trainer")

    # Outsider student cannot read sessions (403)
    assert client.get(f"/api/classes/{class_id}/sessions", headers=other_student_hdr).status_code == 403

    # Outsider trainer cannot read sessions (403)
    assert client.get(f"/api/classes/{class_id}/sessions", headers=other_trainer_hdr).status_code == 403

    # Enrolled student can read sessions of own class (200)
    assert client.get(f"/api/classes/{class_id}/sessions", headers=enrolled_student_hdr).status_code == 200

    # Primary trainer can read sessions (200)
    assert client.get(f"/api/classes/{class_id}/sessions", headers=primary_hdr).status_code == 200


# ---------------------------------------------------------------------------
# 3. Schedule CRUD & Validation tests
# ---------------------------------------------------------------------------


def test_schedule_crud_flow(client: TestClient, db_session: Session):
    class_id = db_session.info["class_id"]
    primary_hdr = _headers(db_session.info["primary_trainer_id"], "trainer")
    admin_hdr = _headers(db_session.info["admin_id"], "admin")

    # 1. GET initial: empty list
    res = client.get(f"/api/classes/{class_id}/schedules", headers=primary_hdr)
    assert res.status_code == 200
    assert res.json() == []

    # 2. POST create schedule: Monday 19:00 - 21:00
    payload = {
        "weekday": 0,
        "start_time": "19:00",
        "end_time": "21:00",
        "effective_from": "2026-09-01",
        "effective_to": None,
    }
    create_res = client.post(f"/api/classes/{class_id}/schedules", json=payload, headers=primary_hdr)
    assert create_res.status_code == 201
    created_sch = create_res.json()
    assert created_sch["weekday"] == 0
    assert created_sch["subject"] == "Computer Science 101"
    assert created_sch["trainer_name"] == "Primary Trainer"
    schedule_id = created_sch["id"]

    # 3. Overlap check: creating another schedule with overlapping hours on same weekday -> 409
    overlap_payload = {
        "weekday": 0,
        "start_time": "20:00",
        "end_time": "22:00",
        "effective_from": "2026-09-01",
    }
    res_overlap = client.post(f"/api/classes/{class_id}/schedules", json=overlap_payload, headers=primary_hdr)
    assert res_overlap.status_code == 409
    assert "overlaps" in res_overlap.json()["detail"].lower()

    # 4. Invalid trainer: assigning an outsider trainer -> 422
    invalid_trainer_payload = {
        "weekday": 1,
        "start_time": "09:00",
        "end_time": "11:00",
        "effective_from": "2026-09-01",
        "trainer_id": str(db_session.info["other_trainer_id"]),
    }
    res_bad_tr = client.post(f"/api/classes/{class_id}/schedules", json=invalid_trainer_payload, headers=primary_hdr)
    assert res_bad_tr.status_code == 422

    # 5. Admin can create schedule for Friday
    admin_payload = {
        "weekday": 4,
        "start_time": "08:00",
        "end_time": "10:00",
        "effective_from": "2026-09-01",
    }
    res_admin = client.post(f"/api/classes/{class_id}/schedules", json=admin_payload, headers=admin_hdr)
    assert res_admin.status_code == 201

    # 6. GET schedules -> returns 2 items
    res_list = client.get(f"/api/classes/{class_id}/schedules", headers=primary_hdr)
    assert res_list.status_code == 200
    assert len(res_list.json()) == 2

    # 7. DELETE schedule
    del_res = client.delete(f"/api/classes/{class_id}/schedules/{schedule_id}", headers=primary_hdr)
    assert del_res.status_code == 200
    assert del_res.json()["ok"] is True

    # 8. DELETE non-existent schedule -> 404
    del_404 = client.delete(f"/api/classes/{class_id}/schedules/{uuid.uuid4()}", headers=primary_hdr)
    assert del_404.status_code == 404


# ---------------------------------------------------------------------------
# 4. Sessions listing & Lazy generation tests
# ---------------------------------------------------------------------------


def test_sessions_lazy_generation_and_idempotency(client: TestClient, db_session: Session):
    class_id = db_session.info["class_id"]
    primary_hdr = _headers(db_session.info["primary_trainer_id"], "trainer")

    # Create Monday schedule
    client.post(
        f"/api/classes/{class_id}/schedules",
        json={
            "weekday": 0,
            "start_time": "19:00",
            "end_time": "21:00",
            "effective_from": "2026-11-01",
        },
        headers=primary_hdr,
    )

    # First GET /sessions in range covering 2 Mondays (2026-11-02 and 2026-11-09)
    res1 = client.get(
        f"/api/classes/{class_id}/sessions?from=2026-11-02&to=2026-11-10",
        headers=primary_hdr,
    )
    assert res1.status_code == 200
    sessions1 = res1.json()
    assert len(sessions1) == 2
    assert sessions1[0]["date"] == "2026-11-02"
    assert sessions1[1]["date"] == "2026-11-09"
    active_enrollments = (
        db_session.query(Enrollment)
        .filter(Enrollment.class_id == class_id, Enrollment.status == "ACTIVE")
        .count()
    )
    assert sessions1[0]["student_count"] == active_enrollments
    assert sessions1[0]["marked_count"] == 0

    first_ids = [s["id"] for s in sessions1]

    # Second GET /sessions in the same range: idempotent, same IDs
    res2 = client.get(
        f"/api/classes/{class_id}/sessions?from=2026-11-02&to=2026-11-10",
        headers=primary_hdr,
    )
    assert res2.status_code == 200
    sessions2 = res2.json()
    assert len(sessions2) == 2
    assert [s["id"] for s in sessions2] == first_ids

    # Invalid range: from > to -> 400
    res_bad = client.get(
        f"/api/classes/{class_id}/sessions?from=2026-11-10&to=2026-11-02",
        headers=primary_hdr,
    )
    assert res_bad.status_code == 400


# ---------------------------------------------------------------------------
# 5. Ad-hoc session creation & Holiday blocking
# ---------------------------------------------------------------------------


def test_create_ad_hoc_session_flow(client: TestClient, db_session: Session):
    class_id = db_session.info["class_id"]
    primary_hdr = _headers(db_session.info["primary_trainer_id"], "trainer")
    student_hdr = _headers(db_session.info["enrolled_student_id"], "student")

    # 1. Student cannot create ad-hoc session (403)
    res_st = client.post(
        f"/api/classes/{class_id}/sessions",
        json={"date": "2026-11-21", "start_time": "19:00", "end_time": "21:00"},
        headers=student_hdr,
    )
    assert res_st.status_code == 403

    # 2. Creating session on holiday is blocked (409)
    res_hol = client.post(
        f"/api/classes/{class_id}/sessions",
        json={"date": "2026-11-20", "start_time": "19:00", "end_time": "21:00"},
        headers=primary_hdr,
    )
    assert res_hol.status_code == 409
    assert "holiday" in res_hol.json()["detail"].lower()

    # 3. Create ad-hoc session on working day -> 201
    res_ok = client.post(
        f"/api/classes/{class_id}/sessions",
        json={
            "date": "2026-11-21",
            "start_time": "19:00",
            "end_time": "21:00",
            "note": "Extra review session",
        },
        headers=primary_hdr,
    )
    assert res_ok.status_code == 201
    body = res_ok.json()
    assert body["schedule_id"] is None
    assert body["date"] == "2026-11-21"
    assert body["note"] == "Extra review session"

    # 4. Duplicate slot collision on same date and start_time -> 409
    res_dup = client.post(
        f"/api/classes/{class_id}/sessions",
        json={"date": "2026-11-21", "start_time": "19:00", "end_time": "21:00"},
        headers=primary_hdr,
    )
    assert res_dup.status_code == 409


# ---------------------------------------------------------------------------
# 6. Delete schedule retains past sessions
# ---------------------------------------------------------------------------


def test_delete_schedule_retains_historical_sessions(client: TestClient, db_session: Session):
    class_id = db_session.info["class_id"]
    primary_hdr = _headers(db_session.info["primary_trainer_id"], "trainer")

    # Create schedule
    sch_res = client.post(
        f"/api/classes/{class_id}/schedules",
        json={
            "weekday": 2,
            "start_time": "14:00",
            "end_time": "16:00",
            "effective_from": "2026-11-01",
        },
        headers=primary_hdr,
    )
    schedule_id = sch_res.json()["id"]

    # Generate session for Wednesday 2026-11-04
    sess_res = client.get(
        f"/api/classes/{class_id}/sessions?from=2026-11-04&to=2026-11-04",
        headers=primary_hdr,
    )
    assert len(sess_res.json()) == 1
    session_id = sess_res.json()[0]["id"]

    # Delete schedule
    del_res = client.delete(f"/api/classes/{class_id}/schedules/{schedule_id}", headers=primary_hdr)
    assert del_res.status_code == 200

    # Query sessions again: Wednesday session still exists
    sess_after = client.get(
        f"/api/classes/{class_id}/sessions?from=2026-11-04&to=2026-11-04",
        headers=primary_hdr,
    )
    assert len(sess_after.json()) == 1
    assert sess_after.json()[0]["id"] == session_id


# ---------------------------------------------------------------------------
# Helper for Attendance Endpoints (6–9)
# ---------------------------------------------------------------------------


def _make_session_helper(
    db: Session,
    class_id: uuid.UUID,
    session_date: date,
    trainer_id: uuid.UUID,
    status: str = "scheduled",
    note: str | None = None,
) -> ClassSession:
    start_at = datetime.combine(session_date, time(9, 0), tzinfo=VIETNAM_TZ)
    end_at = datetime.combine(session_date, time(11, 0), tzinfo=VIETNAM_TZ)
    session = ClassSession(
        class_id=class_id,
        schedule_id=None,
        date=session_date,
        start_at=start_at,
        end_at=end_at,
        trainer_id=trainer_id,
        status=status,
        note=note,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


# ---------------------------------------------------------------------------
# 7. Attendance Sheet & Summary Permissions (Endpoints 6–8)
# ---------------------------------------------------------------------------


def test_attendance_sheet_and_summary_permissions(client: TestClient, db_session: Session):
    class_id = db_session.info["class_id"]
    primary_hdr = _headers(db_session.info["primary_trainer_id"], "trainer")
    co_hdr = _headers(db_session.info["co_trainer_id"], "trainer")
    other_trainer_hdr = _headers(db_session.info["other_trainer_id"], "trainer")
    student_hdr = _headers(db_session.info["enrolled_student_id"], "student")
    admin_hdr = _headers(db_session.info["admin_id"], "admin")

    today = datetime.now(VIETNAM_TZ).date()
    session = _make_session_helper(
        db_session, class_id, today - timedelta(days=2), db_session.info["primary_trainer_id"]
    )

    valid_put_body = {
        "records": [
            {
                "student_id": str(db_session.info["enrolled_student_id"]),
                "status": "present",
                "note": "Attended",
            }
        ]
    }

    # 1. Student cannot read sheet or summary (403)
    res = client.get(
        f"/api/classes/{class_id}/sessions/{session.id}/attendance", headers=student_hdr
    )
    assert res.status_code == 403
    res = client.put(
        f"/api/classes/{class_id}/sessions/{session.id}/attendance",
        json=valid_put_body,
        headers=student_hdr,
    )
    assert res.status_code == 403
    res = client.get(f"/api/classes/{class_id}/attendance/summary", headers=student_hdr)
    assert res.status_code == 403

    # 2. Other trainer (outsider) cannot read sheet or summary (403)
    res = client.get(
        f"/api/classes/{class_id}/sessions/{session.id}/attendance", headers=other_trainer_hdr
    )
    assert res.status_code == 403
    res = client.put(
        f"/api/classes/{class_id}/sessions/{session.id}/attendance",
        json=valid_put_body,
        headers=other_trainer_hdr,
    )
    assert res.status_code == 403
    res = client.get(f"/api/classes/{class_id}/attendance/summary", headers=other_trainer_hdr)
    assert res.status_code == 403

    # 3. Primary trainer, Co-trainer, Admin have full access (200)
    for hdr in [primary_hdr, co_hdr, admin_hdr]:
        get_res = client.get(
            f"/api/classes/{class_id}/sessions/{session.id}/attendance", headers=hdr
        )
        assert get_res.status_code == 200

        sum_res = client.get(f"/api/classes/{class_id}/attendance/summary", headers=hdr)
        assert sum_res.status_code == 200


# ---------------------------------------------------------------------------
# 8. Attendance Sheet GET & PUT Lifecycle (AC-01, AC-02, AC-05)
# ---------------------------------------------------------------------------


def test_attendance_sheet_lifecycle_and_updated_at(client: TestClient, db_session: Session):
    class_id = db_session.info["class_id"]
    primary_hdr = _headers(db_session.info["primary_trainer_id"], "trainer")
    student_id = db_session.info["enrolled_student_id"]

    today = datetime.now(VIETNAM_TZ).date()
    session = _make_session_helper(
        db_session, class_id, today - timedelta(days=2), db_session.info["primary_trainer_id"]
    )

    active_enrollments = (
        db_session.query(Enrollment)
        .filter(Enrollment.class_id == class_id, Enrollment.status == "ACTIVE")
        .count()
    )

    # 1. GET sheet initially: unmarked students, status is None
    get_res = client.get(
        f"/api/classes/{class_id}/sessions/{session.id}/attendance", headers=primary_hdr
    )
    assert get_res.status_code == 200
    data = get_res.json()
    assert data["session"]["id"] == str(session.id)
    assert data["summary"]["unmarked"] == active_enrollments
    assert data["summary"]["present"] == 0
    assert len(data["records"]) == active_enrollments
    record = next(r for r in data["records"] if r["student_id"] == str(student_id))
    assert record["status"] is None
    assert record["updated_at"] is None

    # 2. PUT sheet: mark student as present -> 200, returns updated_at
    put_body = {
        "records": [
            {
                "student_id": str(student_id),
                "status": "present",
                "note": "First check",
            }
        ]
    }
    put_res = client.put(
        f"/api/classes/{class_id}/sessions/{session.id}/attendance",
        json=put_body,
        headers=primary_hdr,
    )
    assert put_res.status_code == 200
    put_data = put_res.json()
    assert put_data["summary"]["present"] == 1
    assert put_data["summary"]["unmarked"] == active_enrollments - 1
    rec = next(r for r in put_data["records"] if r["student_id"] == str(student_id))
    assert rec["status"] == "present"
    assert rec["note"] == "First check"
    first_updated_at = rec["updated_at"]
    assert first_updated_at is not None

    # 3. PUT sheet again: update status to late -> 200, updated_at advances / matches ISO (AC-05)
    put_body2 = {
        "records": [
            {
                "student_id": str(student_id),
                "status": "late",
                "note": "Traffic delay",
            }
        ]
    }
    put_res2 = client.put(
        f"/api/classes/{class_id}/sessions/{session.id}/attendance",
        json=put_body2,
        headers=primary_hdr,
    )
    assert put_res2.status_code == 200
    put_data2 = put_res2.json()
    assert put_data2["summary"]["late"] == 1
    assert put_data2["summary"]["present"] == 0
    assert put_data2["summary"]["unmarked"] == active_enrollments - 1
    rec2 = next(r for r in put_data2["records"] if r["student_id"] == str(student_id))
    assert rec2["status"] == "late"
    assert rec2["note"] == "Traffic delay"
    assert rec2["updated_at"] >= first_updated_at


# ---------------------------------------------------------------------------
# 9. Attendance Sheet PUT Validation Errors (Future, Cancelled, Non-Roster, Duplicate)
# ---------------------------------------------------------------------------


def test_attendance_sheet_put_validations(client: TestClient, db_session: Session):
    class_id = db_session.info["class_id"]
    primary_hdr = _headers(db_session.info["primary_trainer_id"], "trainer")
    enrolled_stu = db_session.info["enrolled_student_id"]
    outsider_stu = db_session.info["other_student_id"]

    today = datetime.now(VIETNAM_TZ).date()

    # Past session for general tests
    past_session = _make_session_helper(
        db_session, class_id, today - timedelta(days=2), db_session.info["primary_trainer_id"]
    )
    # Future session
    future_session = _make_session_helper(
        db_session, class_id, today + timedelta(days=2), db_session.info["primary_trainer_id"]
    )
    # Cancelled session
    cancelled_session = _make_session_helper(
        db_session,
        class_id,
        today - timedelta(days=3),
        db_session.info["primary_trainer_id"],
        status="cancelled",
    )

    # 1. 400 when saving future session (D8)
    res_fut = client.put(
        f"/api/classes/{class_id}/sessions/{future_session.id}/attendance",
        json={"records": [{"student_id": str(enrolled_stu), "status": "present"}]},
        headers=primary_hdr,
    )
    assert res_fut.status_code == 400
    assert "future session" in res_fut.json()["detail"].lower()

    # 2. 409 when saving cancelled session (D9)
    res_canc = client.put(
        f"/api/classes/{class_id}/sessions/{cancelled_session.id}/attendance",
        json={"records": [{"student_id": str(enrolled_stu), "status": "present"}]},
        headers=primary_hdr,
    )
    assert res_canc.status_code == 409
    assert "cancelled session" in res_canc.json()["detail"].lower()

    # 3. 422 when student is not in active roster
    res_out = client.put(
        f"/api/classes/{class_id}/sessions/{past_session.id}/attendance",
        json={"records": [{"student_id": str(outsider_stu), "status": "present"}]},
        headers=primary_hdr,
    )
    assert res_out.status_code == 422
    assert "not an active member" in res_out.json()["detail"].lower()

    # 4. 422 on duplicate student_id in request body
    res_dup = client.put(
        f"/api/classes/{class_id}/sessions/{past_session.id}/attendance",
        json={
            "records": [
                {"student_id": str(enrolled_stu), "status": "present"},
                {"student_id": str(enrolled_stu), "status": "late"},
            ]
        },
        headers=primary_hdr,
    )
    assert res_dup.status_code == 422

    # 5. 404 on non-existent session
    res_404 = client.put(
        f"/api/classes/{class_id}/sessions/{uuid.uuid4()}/attendance",
        json={"records": [{"student_id": str(enrolled_stu), "status": "present"}]},
        headers=primary_hdr,
    )
    assert res_404.status_code == 404


# ---------------------------------------------------------------------------
# 10. Student Attendance History (Endpoint 9, AC-03)
# ---------------------------------------------------------------------------


def test_student_attendance_history_endpoint(client: TestClient, db_session: Session):
    class_id = db_session.info["class_id"]
    primary_hdr = _headers(db_session.info["primary_trainer_id"], "trainer")
    student_hdr = _headers(db_session.info["enrolled_student_id"], "student")
    other_student_hdr = _headers(db_session.info["other_student_id"], "student")
    admin_hdr = _headers(db_session.info["admin_id"], "admin")

    today = datetime.now(VIETNAM_TZ).date()

    # Create 2 past sessions
    s1 = _make_session_helper(
        db_session, class_id, today - timedelta(days=3), db_session.info["primary_trainer_id"]
    )
    s2 = _make_session_helper(
        db_session, class_id, today - timedelta(days=2), db_session.info["primary_trainer_id"]
    )

    # Mark s1 present for enrolled student
    client.put(
        f"/api/classes/{class_id}/sessions/{s1.id}/attendance",
        json={
            "records": [
                {
                    "student_id": str(db_session.info["enrolled_student_id"]),
                    "status": "present",
                    "note": "Attended lecture",
                }
            ]
        },
        headers=primary_hdr,
    )
    # Leave s2 unmarked

    # 1. Enrolled student reads own history -> 200
    res = client.get(
        f"/api/student/classes/{class_id}/attendance?from={today - timedelta(days=7)}&to={today}",
        headers=student_hdr,
    )
    assert res.status_code == 200
    data = res.json()
    assert data["class_id"] == str(class_id)
    assert data["total_sessions"] == 2
    assert data["attended"] == 1
    assert data["attendance_rate"] == 0.5  # 1 present / 2 countable (1 present + 1 unmarked)
    assert len(data["items"]) == 2

    # Check session 1 item
    item1 = next(it for it in data["items"] if it["session_id"] == str(s1.id))
    assert item1["status"] == "present"
    assert item1["note"] == "Attended lecture"

    # Check session 2 item (unmarked past session has status=None)
    item2 = next(it for it in data["items"] if it["session_id"] == str(s2.id))
    assert item2["status"] is None
    assert item2["note"] is None

    # 2. Other student (not enrolled in class) gets 403
    res_other = client.get(
        f"/api/student/classes/{class_id}/attendance",
        headers=other_student_hdr,
    )
    assert res_other.status_code == 403

    # 3. Staff / Admin accessing student history endpoint gets 403 (caller is not enrolled student)
    res_trainer = client.get(
        f"/api/student/classes/{class_id}/attendance",
        headers=primary_hdr,
    )
    assert res_trainer.status_code == 403

    res_admin = client.get(
        f"/api/student/classes/{class_id}/attendance",
        headers=admin_hdr,
    )
    assert res_admin.status_code == 403


