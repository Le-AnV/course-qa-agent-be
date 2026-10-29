"""Comprehensive unit tests for the 3 Scope Decision categories (AI-Scope Gate).

Category 1: Core Curriculum (Direct Course Topics) -> in_scope (Gate returns False)
Category 2: Comparative & Practical Tooling -> in_scope (Gate returns False)
Category 3: Pure Foreign Tech & Off-topic -> out_of_scope (Gate returns True)
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
import pytest
from pydantic import SecretStr

from app.services import decision_service
from app.services.decision_service import ScopeDecision, is_confidently_out_of_scope


@pytest.fixture(autouse=True)
def _clear_cooldown():
    decision_service.reset_failure_state()
    yield
    decision_service.reset_failure_state()


def _mock_gemini(monkeypatch, verdict: str, confidence: float):
    class FakeModels:
        async def generate_content(self, **kwargs):
            return SimpleNamespace(
                parsed=ScopeDecision(verdict=verdict, confidence=confidence),
                text=None,
            )

    class FakeAio:
        def __init__(self):
            self.models = FakeModels()
            self.closed = False

        async def aclose(self):
            self.closed = True

    class FakeClient:
        def __init__(self, **kwargs):
            self.aio = FakeAio()

    monkeypatch.setattr(decision_service.genai, "Client", FakeClient)
    monkeypatch.setattr(decision_service.settings, "decision_provider", "gemini")
    monkeypatch.setattr(decision_service.settings, "google_api_key", SecretStr("secret-key"))
    monkeypatch.setattr(decision_service.settings, "decision_confidence_threshold", 0.8)


@pytest.mark.parametrize(
    "question",
    [
        "Con trỏ (pointer) trong C++ hoạt động như thế nào và cách dùng toán tử & và *?",
        "Sự khác biệt giữa toán tử new/delete trong C++ và malloc/free trong C là gì?",
        "Giải thích tính kế thừa (inheritance) và hàm ảo (virtual function) trong C++ OOP.",
        "Cách sử dụng std::vector trong thư viện STL C++ và push_back vs emplace_back?",
        "Tại sao cần khai báo virtual destructor trong class C++ cơ sở?",
        "Smart pointer (unique_ptr, shared_ptr) trong Modern C++ giúp giải phóng bộ nhớ ra sao?",
        "Cách viết template function và template class trong C++?",
        "Lỗi Segmentation fault trong C++ thường xảy ra khi nào?",
    ],
)
def test_category_1_core_curriculum_passes_gate(monkeypatch, question: str):
    """Core curriculum topics must be classified as in_scope and pass the gate."""
    _mock_gemini(monkeypatch, verdict="in_scope", confidence=1.0)

    blocked = asyncio.run(
        is_confidently_out_of_scope(
            class_name="Lập trình C++",
            class_description="Khóa học C++, OOP, con trỏ, bộ nhớ new/delete, STL",
            question=question,
            history="",
        )
    )
    assert not blocked, f"Expected {question} to NOT be blocked (in-scope)"


@pytest.mark.parametrize(
    "question",
    [
        "So sánh cơ chế quản lý bộ nhớ giữa C++ (manual/RAII) và Python (Garbage Collection).",
        "Sự khác biệt giữa pass-by-reference trong C++ và cơ chế truyền object của Java?",
        "Tại sao C++ thường chạy nhanh hơn Python trong tính toán ma trận?",
        "Cách cài đặt và cấu hình CMake để compile dự án C++ nhiều file trên VS Code?",
        "Lệnh g++ -Wall -Wextra -std=c++17 có ý nghĩa gì khi build chương trình C++?",
        "Cách sử dụng công cụ Valgrind trên Linux để phát hiện memory leak trong C++?",
        "So sánh hiệu năng giữa std::vector của C++ và ArrayList của Java.",
        "Gỡ lỗi (debug) chương trình C++ bằng GDB hoặc VS Code Debugger như thế nào?",
    ],
)
def test_category_2_comparative_and_tooling_passes_gate(monkeypatch, question: str):
    """Comparative questions and practical build/debug tooling are in_scope."""
    _mock_gemini(monkeypatch, verdict="in_scope", confidence=0.98)

    blocked = asyncio.run(
        is_confidently_out_of_scope(
            class_name="Lập trình C++",
            class_description="Khóa học C++, OOP, con trỏ, bộ nhớ new/delete, STL",
            question=question,
            history="",
        )
    )
    assert not blocked, f"Expected comparative/tooling question {question} to NOT be blocked"


@pytest.mark.parametrize(
    "question",
    [
        "Làm thế nào để tạo một React component sử dụng useEffect và useState hooks?",
        "Cách viết decorator trong Python để đo thời gian chạy của một hàm?",
        "Cách cấu hình Docker Compose để chạy PostgreSQL cùng với Redis cache?",
        "Giải thích cú pháp async/await và Promise trong JavaScript ES6.",
        "Cách tạo một API RESTful bằng Spring Boot và kết nối với MySQL trong Java?",
        "Hướng dẫn cách nấu món phở bò truyền thống chuẩn vị Hà Nội?",
        "Dự báo thời tiết Hà Nội và TP.HCM tuần tới có mưa không?",
        "Cách huấn luyện mô hình YOLOv8 để nhận diện vật thể bằng PyTorch?",
        "Tại sao bánh bông lan nướng bằng nồi chiên không dầu bị xẹp sau khi lấy ra?",
    ],
)
def test_category_3_pure_foreign_and_offtopic_blocked(monkeypatch, question: str):
    """Pure foreign technologies (without comparison) and off-topic topics are out_of_scope."""
    _mock_gemini(monkeypatch, verdict="out_of_scope", confidence=1.0)

    blocked = asyncio.run(
        is_confidently_out_of_scope(
            class_name="Lập trình C++",
            class_description="Khóa học C++, OOP, con trỏ, bộ nhớ new/delete, STL",
            question=question,
            history="",
        )
    )
    assert blocked, f"Expected off-topic/pure foreign tech {question} to BE blocked"


def test_low_confidence_verdict_fails_open(monkeypatch):
    """An out_of_scope verdict with confidence below threshold must fail open."""
    _mock_gemini(monkeypatch, verdict="out_of_scope", confidence=0.70)

    blocked = asyncio.run(
        is_confidently_out_of_scope(
            class_name="Lập trình C++",
            class_description="Khóa học C++, OOP",
            question="Có nên dùng C++ cho web server?",
            history="",
        )
    )
    # 0.70 < threshold (0.80) -> must fail open (not blocked)
    assert not blocked



# =============================================================================
# Unit Tests for Query Preprocessing (Structural Detection)
# =============================================================================

def test_preprocess_student_query_with_fenced_code():
    msg = """Cho mình hỏi hàm này bị lỗi memory leak ở đâu:\n```cpp\nvoid process() {\n    int* arr = new int[100];\n}\n```\nCảm ơn bạn!"""
    clean_q, code, has_code = decision_service.preprocess_student_query(msg)
    assert has_code is True
    assert "void process()" in code
    assert "new int[100]" in code
    assert "Cho mình hỏi hàm này bị lỗi memory leak ở đâu:" in clean_q
    assert "Cảm ơn bạn!" in clean_q
    assert "```" not in clean_q


def test_preprocess_student_query_with_compiler_error():
    msg = """Mình chạy thì terminal báo lỗi như sau:\nmain.cpp:12:5: error: 'vector' was not declared in this scope\nGiải thích giúp mình với."""
    clean_q, code, has_code = decision_service.preprocess_student_query(msg)
    assert has_code is True
    assert "error: 'vector' was not declared in this scope" in code
    assert "Giải thích giúp mình với." in clean_q


def test_preprocess_student_query_pure_code():
    msg = """```python\ndef add(a, b): return a + b\n```"""
    clean_q, code, has_code = decision_service.preprocess_student_query(msg)
    assert has_code is True
    assert "def add(a, b): return a + b" in code
    assert clean_q == "Explain and debug the following code"


def test_preprocess_student_query_normal_text():
    msg = "Khái niệm đa hình (polymorphism) trong C++ là gì?"
    clean_q, code, has_code = decision_service.preprocess_student_query(msg)
    assert has_code is False
    assert code == ""
    assert clean_q == msg


# =============================================================================
# 20-Row Decision Truth Table Routing Tests
# =============================================================================

import uuid
from unittest.mock import MagicMock
from app.services import chat_retrieval_service, chat_service, rag_service
from app.services.decision_service import ScopeVerdict


def _make_retrieval_decision(score: float | None, source: str, cluster_count: int = 1) -> rag_service.RetrievalDecision:
    chunks = ()
    if score is not None:
        chunks = (
            rag_service.ScoredChunk(
                content="Sample slide passage on C++ concepts.",
                score=score,
                metadata={"document_id": "doc-1", "file_name": "slide.pdf", "page_number": 1},
            ),
        )
    return rag_service.RetrievalDecision(
        question="test question",
        class_id=str(uuid.uuid4()),
        threshold=0.65,
        unrelated_threshold=0.30,
        top_score=score,
        source=source,
        chunks=chunks,
        cluster_count=cluster_count,
    )


async def _collect_events(stream):
    events = []
    async for raw in stream:
        lines = raw.strip().splitlines()
        event_name = lines[0].removeprefix("event: ")
        import json
        event_data = json.loads(lines[1].removeprefix("data: "))
        events.append((event_name, event_data))
    return events


@pytest.mark.parametrize(
    "verdict,zone,score,has_code_in_msg,is_hw,expected_source,expected_used_web",
    [
        # Row 1: CORE x High x No Code x No HW -> DOCS (curriculum)
        ("core", "high", 0.85, False, False, rag_service.SOURCE_CURRICULUM, False),
        # Row 2: CORE x High x No Code x Is HW -> DOCS_SOCRATIC (curriculum)
        ("core", "high", 0.85, False, True, rag_service.SOURCE_CURRICULUM, False),
        # Row 3: CORE x High x Has Code -> CODE_HELP (curriculum)
        ("core", "high", 0.85, True, False, rag_service.SOURCE_CURRICULUM, False),
        # Row 4: CORE x Mid x cluster>=1 -> DOCS (curriculum)
        ("core", "mid", 0.50, False, False, rag_service.SOURCE_CURRICULUM, False),
        # Row 5: CORE x Mid x cluster==0 -> WEB_SEARCH (internet)
        ("core", "mid_low_cluster", 0.50, False, False, rag_service.SOURCE_INTERNET, True),
        # Row 6: CORE x Low x No Code x No HW -> WEB_SEARCH (materials insufficient, fall back to web)
        ("core", "low", 0.20, False, False, rag_service.SOURCE_INTERNET, True),
        # Row 7: CORE x Low x No Code x Is HW -> DOCS_SOCRATIC (curriculum)
        ("core", "low", 0.20, False, True, rag_service.SOURCE_CURRICULUM, False),
        # Row 8: CORE x Low x Has Code -> CODE_HELP (curriculum, bypasses low score!)
        ("core", "low", 0.20, True, False, rag_service.SOURCE_CURRICULUM, False),
        # Row 9: COMPARISON x High -> DOCS_PLUS_WEB
        ("comparison", "high", 0.85, False, False, "docs_plus_web", True),
        # Row 10: COMPARISON x Mid -> DOCS_PLUS_WEB
        ("comparison", "mid", 0.50, False, False, "docs_plus_web", True),
        # Row 11: COMPARISON x Low -> DOCS_PLUS_WEB
        ("comparison", "low", 0.20, False, False, "docs_plus_web", True),
        # Row 12: EXTENSION x High -> DOCS
        ("extension", "high", 0.85, False, False, rag_service.SOURCE_CURRICULUM, False),
        # Row 13: EXTENSION x Mid -> WEB_SEARCH
        ("extension", "mid", 0.50, False, False, rag_service.SOURCE_INTERNET, True),
        # Row 14: EXTENSION x Low -> WEB_SEARCH
        ("extension", "low", 0.20, False, False, rag_service.SOURCE_INTERNET, True),
        # Row 15: LOGISTICS -> logistics
        ("logistics", "low", 0.20, False, False, "logistics", False),
        # Row 16: OUT_OF_SCOPE (tech) -> reject
        ("out_of_scope_tech", "low", 0.10, False, False, rag_service.SOURCE_UNRELATED, False),
        # Row 17: OUT_OF_SCOPE (general) -> reject
        ("out_of_scope_gen", "low", 0.10, False, False, rag_service.SOURCE_UNRELATED, False),
        # Row 18: GATE_ERROR x High -> fail open DOCS
        ("gate_error", "high", 0.85, False, False, rag_service.SOURCE_CURRICULUM, False),
        # Row 19: GATE_ERROR x Mid -> fail open DOCS
        ("gate_error", "mid", 0.50, False, False, rag_service.SOURCE_CURRICULUM, False),
        # Row 20: GATE_ERROR x Low -> SAFE_FALLBACK (NO WEB!)
        ("gate_error", "low", 0.20, False, False, rag_service.SOURCE_UNRELATED, False),
    ],
)
def test_decision_truth_table_20_scenarios(
    monkeypatch,
    verdict: str,
    zone: str,
    score: float,
    has_code_in_msg: bool,
    is_hw: bool,
    expected_source: str,
    expected_used_web: bool,
):
    """Verify that every row of the 20-row Decision Truth Table executes the exact expected action."""
    test_class_id = uuid.uuid4()

    monkeypatch.setattr(
        chat_service.course_service,
        "get_class",
        lambda *args: SimpleNamespace(
            name="Lập trình C++",
            info=SimpleNamespace(description="Khóa học lập trình C++ OOP"),
        ),
    )

    class FakeModel:
        async def astream(self, messages):
            yield SimpleNamespace(content="Sample generated response.")

    # Setup retrieval mock
    source_map = {
        "high": rag_service.SOURCE_CURRICULUM,
        "mid": rag_service.SOURCE_CURRICULUM,
        "mid_low_cluster": rag_service.SOURCE_INTERNET,
        "low": rag_service.SOURCE_UNRELATED,
    }
    cluster_cnt = 0 if zone == "mid_low_cluster" else 2
    r_decision = _make_retrieval_decision(score, source_map[zone], cluster_count=cluster_cnt)
    monkeypatch.setattr(chat_retrieval_service, "retrieve_for_chat", lambda *args, **kwargs: r_decision)

    # Setup web search mock (returns 1 sample web result if called)
    web_called = []
    def fake_web_search(q):
        web_called.append(q)
        return [rag_service.WebResult(title="Web Result", url="https://example.com", content="Web content")]
    monkeypatch.setattr(rag_service, "search_internet", fake_web_search)

    # Setup Gatekeeper mock
    if verdict == "gate_error":
        async def exploding_gate(**kwargs):
            raise RuntimeError("Gemini 503 Service Unavailable")
        monkeypatch.setattr(chat_service.decision_service, "decide_scope", exploding_gate)
    else:
        v_actual = verdict
        is_rel_tech = False
        if verdict == "out_of_scope_tech":
            v_actual = "out_of_scope"
            is_rel_tech = True
        elif verdict == "out_of_scope_gen":
            v_actual = "out_of_scope"
            is_rel_tech = False

        decision_obj = ScopeDecision(
            verdict=v_actual,
            confidence=0.95,
            is_homework_request=is_hw,
            is_related_tech=is_rel_tech,
            external_search_query="external tech query" if v_actual == "comparison" else None,
        )
        async def fake_gate(**kwargs):
            return decision_obj
        monkeypatch.setattr(chat_service.decision_service, "decide_scope", fake_gate)

    # Build student query message
    msg = "Khái niệm con trỏ"
    if has_code_in_msg:
        msg = "Đoạn code này bị lỗi gì:\n```cpp\nint* p = nullptr;\n*p = 10;\n```"

    events = asyncio.run(
        _collect_events(
            chat_service.stream_chat(
                db=MagicMock(),
                message=msg,
                class_id=test_class_id,
                model=FakeModel(),
                timeout_seconds=2,
            )
        )
    )

    done_event = [data for evt, data in events if evt == "done"][0]
    assert done_event["source"] == expected_source, f"Expected source {expected_source}, got {done_event['source']}"
    assert done_event["used_web_search"] is expected_used_web, f"Expected used_web_search={expected_used_web}, got {done_event['used_web_search']}"

    # Critical Assertion: Low-zone fail-open MUST NOT burn SerpApi quota
    if verdict == "gate_error" and zone == "low":
        assert len(web_called) == 0, "Gate error in Low Zone must NOT call SerpApi!"
