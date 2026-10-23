"""MSSV (student_code) rules: INITIALS_NNNN auto-generation, explicit codes, import."""

import io

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.main import app
from app.models.user import User
from app.services import import_service, user_service

TEST_DB_URL = "sqlite://"


@pytest.fixture()
def db_session(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import app.core.database as dbmod

    reset_engine()
    monkeypatch.setattr(settings, "database_url", TEST_DB_URL)
    monkeypatch.setattr(settings, "email_dry_run", True)

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
        from app.core.seed import INITIAL_ROLES
        from app.models.user import Role

        for role_data in INITIAL_ROLES:
            if db.query(Role).filter(Role.name == role_data["name"]).first() is None:
                db.add(Role(name=role_data["name"], description=role_data["description"]))
        db.commit()
        yield db
    finally:
        db.close()
        engine.dispose()
    reset_engine()


@pytest.fixture()
def api_client(db_session):
    def _override():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def admin_headers(client: TestClient) -> dict:
    res = client.post(
        "/api/auth/login",
        json={"email": settings.bootstrap_admin_email, "password": settings.bootstrap_admin_password},
    )
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def make_xlsx(rows: list[list[str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["fullname", "email", "type", "mssv"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_xlsx5(rows: list[list[str]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["fullname", "email", "type", "mssv", "msnv"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_initials_vietnamese_names():
    assert user_service._initials("Tran Van A") == "TVA"
    assert user_service._initials("Trần Văn A") == "TVA"
    assert user_service._initials("Nguyễn Thị Minh Khai") == "NTMK"
    assert user_service._initials("  An   ") == "A"


def test_auto_code_matches_format(db_session):
    code = user_service.generate_student_code(db_session, "Tran Van A")
    assert user_service.STUDENT_CODE_RE.fullmatch(code), code
    assert code.startswith("TVA_")
    # Second one is random, not the previous + 1.
    assert user_service.generate_student_code(db_session, "Tran Van A").startswith("TVA_")


def test_auto_codes_are_random_and_unique_per_prefix(db_session):
    names = ["Tran Van A", "Tran Van Anh", "Tran Thi B", "Tran Van Binh"]
    for i, name in enumerate(names):
        user_service.create_user(db_session, name, f"a{i}@gmail.com", "student", None)
    user_service.create_user(db_session, "Le Binh", "b1@gmail.com", "student", None)

    codes = {u.email: u.student_code for u in db_session.query(User).all()}
    assert all(user_service.STUDENT_CODE_RE.fullmatch(c) for c in codes.values())
    # Each initials prefix keeps its own random namespace.
    assert {c.split("_")[0] for c in codes.values()} == {"TVA", "TTB", "TVB", "LB"}
    assert len(set(codes.values())) == len(codes), "MSSV must be unique"
    assert codes["b1@gmail.com"].startswith("LB_")


def test_auto_code_avoids_existing(db_session):
    user_service.create_user(db_session, "Tran Van A", "a@gmail.com", "student", None)
    first = db_session.query(User).filter(User.email == "a@gmail.com").first().student_code
    user_service.create_user(db_session, "Tran Van A", "a2@gmail.com", "student", None)
    second = db_session.query(User).filter(User.email == "a2@gmail.com").first().student_code
    assert first != second


def test_explicit_code_and_duplicates(db_session):
    user, _ = user_service.create_user(
        db_session, "Tran Van A", "a@gmail.com", "student", None, student_code="TVA_0042"
    )
    assert user.student_code == "TVA_0042"
    with pytest.raises(user_service.DomainError, match="already exists"):
        user_service.create_user(
            db_session, "Tran Van B", "b@gmail.com", "student", None, student_code="tva_0042"
        )
    with pytest.raises(user_service.DomainError, match="Invalid student code"):
        user_service.create_user(
            db_session, "Tran Van C", "c@gmail.com", "student", None, student_code="bad-code"
        )


def test_no_code_for_staff(db_session):
    user, _ = user_service.create_user(db_session, "T", "t@gmail.com", "trainer", None)
    assert user.student_code is None
    with pytest.raises(user_service.DomainError, match="only for student"):
        user_service.create_user(
            db_session, "T2", "t2@gmail.com", "trainer", None, student_code="T_0001"
        )


def test_import_auto_and_explicit_mssv(api_client):
    h = admin_headers(api_client)
    payload = make_xlsx(
        [
            ["Tran Van A", "mssv1@gmail.com", "student", ""],
            ["Tran Van Anh", "mssv2@gmail.com", "student", "TVA_0099"],
            ["Bad Code", "mssv3@gmail.com", "student", "nope"],
            ["Dup Code", "mssv4@gmail.com", "student", "TVA_0099"],
        ]
    )
    up = api_client.post("/api/admin/imports", files={"file": ("a.xlsx", payload)}, headers=h)
    assert up.status_code == 201, up.text
    body = up.json()
    assert (body["imported_count"], body["skipped_count"], body["failed_count"]) == (2, 0, 2)

    detail = api_client.get(f"/api/admin/imports/{body['id']}", headers=h).json()
    by_email = {r["email"]: r for r in detail["rows"]}
    assert by_email["mssv1@gmail.com"]["status"] == "imported"
    auto_code = by_email["mssv1@gmail.com"]["student_code"]
    assert user_service.STUDENT_CODE_RE.fullmatch(auto_code or ""), auto_code
    assert auto_code in by_email["mssv1@gmail.com"]["message"]
    assert by_email["mssv2@gmail.com"]["student_code"] == "TVA_0099"
    assert by_email["mssv3@gmail.com"]["status"] == "failed"
    assert by_email["mssv4@gmail.com"]["status"] == "failed"

    me = api_client.get("/api/admin/users", headers=h).json()
    codes = {u["email"]: u["student_code"] for u in me}
    assert codes["mssv1@gmail.com"] == auto_code
    assert codes["mssv2@gmail.com"] == "TVA_0099"


def test_import_staff_msnv(api_client):
    h = admin_headers(api_client)
    payload = make_xlsx5(
        [
            ["Nguyen Van T", "msnv1@gmail.com", "trainer", "", "fpt01234"],
            ["Tran Van B", "msnv2@gmail.com", "student", "TVA_0123", "FPT99999"],
            ["Nguyen Van U", "msnv3@gmail.com", "trainer", "", "FPT01234"],
        ]
    )
    up = api_client.post("/api/admin/imports", files={"file": ("staff.xlsx", payload)}, headers=h)
    assert up.status_code == 201, up.text
    body = up.json()
    assert (body["imported_count"], body["skipped_count"], body["failed_count"]) == (1, 0, 2)

    detail = api_client.get(f"/api/admin/imports/{body['id']}", headers=h).json()
    by_email = {r["email"]: r for r in detail["rows"]}
    assert by_email["msnv1@gmail.com"]["staff_code"] == "FPT01234"
    assert "MSNV FPT01234" in by_email["msnv1@gmail.com"]["message"]
    assert by_email["msnv2@gmail.com"]["status"] == "failed"
    assert by_email["msnv3@gmail.com"]["status"] == "failed"

    users = api_client.get("/api/admin/users", headers=h).json()
    staff_codes = {u["email"]: u["staff_code"] for u in users}
    assert staff_codes["msnv1@gmail.com"] == "FPT01234"


def test_template_has_guide_sheet(api_client):
    from openpyxl import load_workbook

    h = admin_headers(api_client)
    tpl = api_client.get("/api/admin/import-template", headers=h)
    assert tpl.status_code == 200
    book = load_workbook(filename=io.BytesIO(tpl.content), read_only=True)
    assert book.sheetnames[0] == "accounts"
    guide = book["Hướng dẫn"]
    header = [c for c in next(guide.iter_rows(values_only=True))]
    assert header[0] == "Cột"
    texts = " ".join(str(c or "") for row in guide.iter_rows(values_only=True) for c in row)
    assert "mssv" in texts.lower()
    assert "msnv" in texts.lower()
    assert "password" in texts.lower()


def test_old_template_rejected(api_client):
    h = admin_headers(api_client)
    wb = Workbook()
    ws = wb.active
    ws.append(["username", "gmail", "type"])
    ws.append(["Stu", "old@gmail.com", "student"])
    buf = io.BytesIO()
    wb.save(buf)
    up = api_client.post("/api/admin/imports", files={"file": ("old.xlsx", buf.getvalue())}, headers=h)
    assert up.status_code == 400
    assert "fullname, email, type, mssv" in up.json()["detail"]


def test_import_service_unit_parse(db_session):
    header, rows = import_service.parse_workbook(import_service.build_template_bytes())
    assert header == ["fullname", "email", "type", "mssv", "msnv"]
    assert len(rows) == 1  # example row only
    assert rows[0]["fullname"] == "Nguyen Van A"
    assert rows[0]["msnv"] == ""
    assert import_service.validate_row(db_session, rows[0]) is None

def test_staff_code_for_trainer(db_session):
    trainer, _ = user_service.create_user(
        db_session, "Nguyen Van T", "t3@gmail.com", "trainer", None, staff_code="FPT01234"
    )
    assert trainer.staff_code == "FPT01234"
    assert trainer.student_code is None


def test_staff_code_is_unique_and_normalized(db_session):
    user_service.create_user(
        db_session, "Nguyen Van T", "t4@gmail.com", "trainer", None, staff_code=" fpt05678 "
    )
    assert (
        db_session.query(User).filter(User.email == "t4@gmail.com").first().staff_code
        == "FPT05678"
    )
    with pytest.raises(user_service.DomainError, match="already exists"):
        user_service.create_user(
            db_session, "Nguyen Van U", "u4@gmail.com", "trainer", None,
            staff_code="fpt05678",
        )


def test_staff_code_rejected_for_student(db_session):
    with pytest.raises(user_service.DomainError, match="only for staff"):
        user_service.create_user(
            db_session, "Tran Van B", "b3@gmail.com", "student", None,
            staff_code="FPT09999",
        )
