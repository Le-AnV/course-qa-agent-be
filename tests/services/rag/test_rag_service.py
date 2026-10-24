"""AI-65 — retrieval score threshold: KB vs Internet (AC-01..AC-05).

The dispatcher is exercised with a fake vector store and a fake Internet agent,
so these tests never call Gemini, Qdrant, or Tavily. A real in-memory SQLite
database proves that the threshold is admin-configurable and applies to the
next question, and that AI-39's deleted/replaced-document guard is honoured.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from langchain_core.documents import Document
from sqlalchemy.orm import Session

from app import models  # noqa: F401  (register tables)
from app.config import settings
from app.core.database import Base, get_session_local, reset_engine
from app.core.deps import get_db
from app.core.seed import seed_database
from app.main import app
from app.models.course import Class
from app.models.document import Document as DocumentRow
from app.models.system_config import SystemConfig
from app.models.user import User
from app.services import rag_service

TEST_DB_URL = "sqlite://"


def _build_session(monkeypatch) -> Session:
    """Point the app at a fresh in-memory SQLite database and seed users/roles."""

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
    return get_session_local(TEST_DB_URL)()


@pytest.fixture()
def db_session(monkeypatch):
    db = _build_session(monkeypatch)
    try:
        seed_database(db)
        yield db
    finally:
        db.close()
        reset_engine()


def _seed_class_and_document(
    db: Session,
    *,
    status: str = "indexed",
    file_name: str = "syllabus.pdf",
) -> tuple[Class, DocumentRow]:
    teacher = db.query(User).filter(User.email == "teacher@academic.edu.vn").first()
    clazz = Class(
        trainer_id=teacher.id,
        code=f"CS101-{uuid.uuid4().hex[:6]}",
        name="CS101 - Retrieval",
        semester="HK1",
        academic_year="2026-2027",
        status="ACTIVE",
    )
    db.add(clazz)
    db.flush()
    document = DocumentRow(
        class_id=clazz.id,
        uploaded_by=teacher.id,
        file_name=file_name,
        file_type=".pdf",
        mime_type="application/pdf",
        size_bytes=120,
        status=status,
    )
    db.add(document)
    db.commit()
    db.refresh(clazz)
    db.refresh(document)
    return clazz, document


class FakeStore:
    """Return a canned top-k list and record the class-scoped search call."""

    def __init__(self, pairs: list[tuple[Document, float]] | Exception):
        self.pairs = pairs
        self.calls: list[dict] = []

    def similarity_search_with_score(self, query, k=None, *, class_id=None):
        """Mimic QdrantStore's scored, class-filtered contract."""

        self.calls.append({"query": query, "k": k, "class_id": class_id})
        if isinstance(self.pairs, Exception):
            raise self.pairs
        return self.pairs


class FakeInternetAgent:
    """Deterministic T19 stand-in: records the question, returns fixed sources."""

    def __init__(self, results: list[rag_service.WebResult] | Exception):
        self.results = results
        self.questions: list[str] = []

    def __call__(self, question: str):
        """Return the canned results for the hand-off assertion."""

        self.questions.append(question)
        if isinstance(self.results, Exception):
            raise self.results
        return self.results


def _chunk(
    content: str = "Lecture notes about binary search trees.",
    *,
    score: float,
    document_id: str,
    file_name: str = "syllabus.pdf",
    page: int = 4,
) -> tuple[Document, float]:
    return (
        Document(
            page_content=content,
            metadata={
                "document_id": document_id,
                "file_name": file_name,
                "page_number": page,
                "class_id": "unused-by-the-fake-store",
            },
        ),
        score,
    )


def _answer_text(system_prompt: str, user_prompt: str) -> str:
    """Stand-in LLM that echoes the numbered source markers it received."""

    assert "sources" in (system_prompt + user_prompt).lower()
    return "Grounded answer [1]."


# --- AC-05: admin-configurable threshold -----------------------------------


def test_threshold_defaults_to_documented_0_8(db_session):
    """No stored row -> AI-107's documented default 0.8 (AC-01, AC-05)."""

    assert rag_service.get_retrieval_threshold(db_session) == 0.8
    config = rag_service.retrieval_threshold_config(db_session)
    assert config["threshold"] == 0.8
    assert config["default_threshold"] == 0.8
    assert config["is_default"] is True


def test_threshold_is_persisted_and_applies_to_next_question(db_session):
    """Saving 0.65 stores it in system_config and reads it back (AC-05)."""

    rag_service.save_retrieval_threshold(db_session, 0.65)

    row = (
        db_session.query(SystemConfig)
        .filter(SystemConfig.key == rag_service.RETRIEVAL_THRESHOLD_KEY)
        .first()
    )
    assert row is not None
    assert float(row.value) == 0.65
    assert rag_service.get_retrieval_threshold(db_session) == 0.65

    config = rag_service.retrieval_threshold_config(db_session)
    assert config["threshold"] == 0.65
    assert config["is_default"] is False


def test_invalid_stored_threshold_falls_back_to_default(db_session):
    """A corrupted row must never break chat (defensive read)."""

    db_session.add(SystemConfig(key=rag_service.RETRIEVAL_THRESHOLD_KEY, value="not-a-number"))
    db_session.commit()

    assert rag_service.get_retrieval_threshold(db_session) == 0.8


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf")])
def test_save_rejects_out_of_range_threshold(db_session, value):
    """Threshold must stay a probability-like score in [0, 1]."""

    with pytest.raises(rag_service.RetrievalThresholdError):
        rag_service.save_retrieval_threshold(db_session, value)


# --- AC-01 / AC-02: retrieval first, curriculum at/above threshold ---------


def test_retrieval_always_runs_class_scoped_before_routing(db_session):
    """AC-01: retrieval runs first with the caller's class id."""

    clazz, document = _seed_class_and_document(db_session)
    store = FakeStore([_chunk(score=0.91, document_id=str(document.id))])

    decision = rag_service.decide_source(
        db_session,
        question="What is a binary tree?",
        class_id=clazz.id,
        store=store,
    )

    assert store.calls == [
        {
            "query": "What is a binary tree?",
            "k": settings.retriever_top_k,
            "class_id": str(clazz.id),
        }
    ]
    assert decision.source == rag_service.SOURCE_CURRICULUM
    assert decision.top_score == 0.91


def test_score_equal_to_threshold_answers_from_curriculum(db_session):
    """AC-02: `>=` is inclusive at the boundary."""

    clazz, document = _seed_class_and_document(db_session)
    store = FakeStore([_chunk(score=0.8, document_id=str(document.id))])

    decision = rag_service.decide_source(
        db_session, question="Boundary?", class_id=clazz.id, store=store
    )

    assert decision.threshold == 0.8
    assert decision.source == rag_service.SOURCE_CURRICULUM


def test_curriculum_answer_carries_citations(db_session):
    """AC-02 + E5: the answer is grounded in the chunk and cites the file/page."""

    clazz, document = _seed_class_and_document(db_session)
    store = FakeStore([_chunk(score=0.94, document_id=str(document.id))])
    captured: dict[str, str] = {}

    def generate(system_prompt: str, user_prompt: str) -> str:
        captured["prompt"] = user_prompt
        return "Binary trees have at most two children [1]."

    answer = rag_service.answer_question(
        db_session,
        question="What is a binary tree?",
        class_id=clazz.id,
        store=store,
        generate=generate,
    )

    assert answer.source == rag_service.SOURCE_CURRICULUM
    assert answer.used_web_search is False
    assert answer.answer == "Binary trees have at most two children [1]."
    assert "[1] syllabus.pdf, page 4" in captured["prompt"]
    assert len(answer.citations) == 1
    citation = answer.citations[0]
    assert citation.kind == rag_service.SOURCE_CURRICULUM
    assert citation.title == "syllabus.pdf"
    assert citation.document_id == str(document.id)
    assert citation.page == 4
    assert citation.url is None


# --- AC-03: below threshold hands off to the Internet agent ----------------


def test_score_below_threshold_hands_question_to_internet_agent(db_session):
    """AC-03: low KB confidence -> T19 agent, never a refusal/ticket."""

    clazz, document = _seed_class_and_document(db_session)
    store = FakeStore([_chunk(score=0.55, document_id=str(document.id))])
    agent = FakeInternetAgent(
        [
            rag_service.WebResult(
                title="Python docs — data structures",
                url="https://docs.python.org/3/tutorial/datastructures.html",
                content="Lists and dictionaries are built-in data structures.",
            )
        ]
    )

    def generate(system_prompt: str, user_prompt: str) -> str:
        assert "Internet Sources" in user_prompt
        return "Lists are built-in [1] (https://docs.python.org/3/tutorial/datastructures.html)."

    answer = rag_service.answer_question(
        db_session,
        question="What data structures does Python have?",
        class_id=clazz.id,
        store=store,
        internet_search=agent,
        generate=generate,
    )

    assert store.calls, "retrieval must still run first (AC-01)"
    assert agent.questions == ["What data structures does Python have?"]
    assert answer.source == rag_service.SOURCE_INTERNET
    assert answer.used_web_search is True
    assert answer.top_score == 0.55
    assert answer.citations[0].kind == rag_service.SOURCE_INTERNET
    assert answer.citations[0].url.startswith("https://docs.python.org/")


def test_threshold_change_switches_the_same_score_between_sources(db_session):
    """AC-05: raising/lowering the threshold changes the next question's route."""

    clazz, document = _seed_class_and_document(db_session)
    store = FakeStore([_chunk(score=0.75, document_id=str(document.id))])
    agent = FakeInternetAgent(
        [rag_service.WebResult("Web", "https://example.com", "Web content")]
    )

    low_confidence = rag_service.decide_source(
        db_session, question="Same score", class_id=clazz.id, store=store
    )
    assert low_confidence.source == rag_service.SOURCE_INTERNET

    rag_service.save_retrieval_threshold(db_session, 0.7)

    high_confidence = rag_service.decide_source(
        db_session, question="Same score", class_id=clazz.id, store=store
    )
    assert high_confidence.source == rag_service.SOURCE_CURRICULUM

    # And the Internet agent is not called when the curriculum wins.
    rag_service.answer_question(
        db_session,
        question="Same score",
        class_id=clazz.id,
        store=store,
        internet_search=agent,
        generate=_answer_text,
    )
    assert agent.questions == []


def test_retrieval_outage_falls_back_to_internet_instead_of_refusing(db_session):
    """AC-03: a Qdrant/embedding outage must not return an error to the student."""

    clazz, _ = _seed_class_and_document(db_session)
    store = FakeStore(RuntimeError("Qdrant is down"))
    agent = FakeInternetAgent(
        [rag_service.WebResult("Web", "https://example.com", "Web content")]
    )

    answer = rag_service.answer_question(
        db_session,
        question="Still answer me",
        class_id=clazz.id,
        store=store,
        internet_search=agent,
        generate=_answer_text,
    )

    assert answer.source == rag_service.SOURCE_INTERNET
    assert answer.top_score is None
    assert answer.used_web_search is True


def test_internet_agent_outage_returns_no_source_message(db_session):
    """AC-03 + AC-04: no source at all -> a normal message, never an invented answer."""

    clazz, document = _seed_class_and_document(db_session)
    store = FakeStore([_chunk(score=0.55, document_id=str(document.id))])

    def generate(system_prompt: str, user_prompt: str) -> str:
        raise AssertionError("LLM must not be called without a source (AC-04)")

    answer = rag_service.answer_question(
        db_session,
        question="Unanswerable?",
        class_id=clazz.id,
        store=store,
        internet_search=FakeInternetAgent(RuntimeError("provider down")),
        generate=generate,
    )

    assert answer.answer == rag_service.NO_SOURCE_MESSAGE
    assert answer.used_web_search is True
    assert answer.citations == ()


def test_score_below_unrelated_threshold_does_not_call_internet(db_session):
    """Below unrelated threshold (Tier 3) -> out of scope; reuse no source message without calling Internet."""

    clazz, document = _seed_class_and_document(db_session)
    store = FakeStore([_chunk(score=0.2, document_id=str(document.id))])
    agent = FakeInternetAgent([])

    answer = rag_service.answer_question(
        db_session,
        question="Unrelated question?",
        class_id=clazz.id,
        store=store,
        internet_search=agent,
        generate=lambda s, u: "invented",
    )

    assert store.calls
    assert not agent.questions, "Internet search must not be called when question is unrelated"
    assert answer.source == rag_service.SOURCE_UNRELATED
    assert answer.answer == rag_service.NO_SOURCE_MESSAGE
    assert answer.used_web_search is False


def test_generation_failure_returns_only_cited_sources(db_session):
    """AC-04: if Gemini fails, quote the retrieved source instead of guessing."""

    clazz, document = _seed_class_and_document(db_session)
    store = FakeStore([_chunk(score=0.99, document_id=str(document.id))])

    def generate(system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("Gemini quota exhausted")

    answer = rag_service.answer_question(
        db_session,
        question="What is in the syllabus?",
        class_id=clazz.id,
        store=store,
        generate=generate,
    )

    assert answer.source == rag_service.SOURCE_CURRICULUM
    assert answer.answer.startswith("The answer could not be generated")
    assert "[1] syllabus.pdf" in answer.answer
    assert answer.citations[0].title == "syllabus.pdf"


# --- AI-39 integration: deleted / replaced documents cannot answer ---------


@pytest.mark.parametrize("status", ["replaced", "rejected", "failed", "pending"])
def test_chunks_of_non_retrievable_documents_are_dropped(db_session, status):
    """A stale Qdrant point must not win the threshold decision."""

    clazz, document = _seed_class_and_document(db_session, status=status)
    store = FakeStore([_chunk(score=0.99, document_id=str(document.id))])

    decision = rag_service.decide_source(
        db_session, question="Old material?", class_id=clazz.id, store=store
    )

    assert decision.top_score is None
    assert decision.source == rag_service.SOURCE_INTERNET


def test_unverifiable_chunks_without_document_id_are_dropped(db_session):
    """No document_id -> cannot prove the file is live -> never cited (AI-39)."""

    clazz, _ = _seed_class_and_document(db_session)
    orphan = Document(page_content="orphan chunk", metadata={"file_name": "ghost.pdf"})
    store = FakeStore([(orphan, 0.99)])

    decision = rag_service.decide_source(
        db_session, question="Who wrote this?", class_id=clazz.id, store=store
    )

    assert decision.top_score is None
    assert decision.source == rag_service.SOURCE_INTERNET


# --- Admin API (AC-05) ------------------------------------------------------


@pytest.fixture()
def client(db_session):
    def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _login_headers(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def admin_headers(client):
    return _login_headers(client, "admin@academic.edu.vn", "Admin@123")


@pytest.fixture()
def trainer_headers(client):
    return _login_headers(client, "teacher@academic.edu.vn", "Teacher@123")


@pytest.fixture()
def student_headers(client):
    return _login_headers(client, "student@academic.edu.vn", "Student@123")


def test_admin_reads_default_retrieval_threshold(client, admin_headers):
    response = client.get("/api/admin/retrieval-config", headers=admin_headers)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "threshold": 0.8,
        "default_threshold": 0.8,
        "is_default": True,
        "updated_at": None,
        "unrelated_threshold": 0.45,
        "default_unrelated_threshold": 0.45,
    }


def test_admin_updates_retrieval_threshold(client, admin_headers):
    response = client.patch(
        "/api/admin/retrieval-config",
        headers=admin_headers,
        json={"threshold": 0.65},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["threshold"] == 0.65
    assert body["is_default"] is False
    assert body["updated_at"] is not None

    reread = client.get("/api/admin/retrieval-config", headers=admin_headers)
    assert reread.json()["threshold"] == 0.65


@pytest.mark.parametrize("threshold", [-0.1, 1.1])
def test_admin_threshold_outside_range_is_rejected(client, admin_headers, threshold):
    response = client.patch(
        "/api/admin/retrieval-config",
        headers=admin_headers,
        json={"threshold": threshold},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("role_fixture", ["trainer_headers", "student_headers"])
def test_non_admin_cannot_read_or_change_threshold(client, request, role_fixture):
    headers = request.getfixturevalue(role_fixture)

    read = client.get("/api/admin/retrieval-config", headers=headers)
    write = client.patch(
        "/api/admin/retrieval-config", headers=headers, json={"threshold": 0.5}
    )

    assert read.status_code == 403
    assert write.status_code == 403


def test_threshold_config_requires_authentication(client):
    assert client.get("/api/admin/retrieval-config").status_code == 401
