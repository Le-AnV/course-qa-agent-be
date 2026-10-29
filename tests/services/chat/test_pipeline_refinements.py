"""Unit tests for the refined chat pipeline routing, code handling, and fallback logic."""

from __future__ import annotations

from app.services import chat_service, decision_service, rag_service


def test_preprocess_student_query_code_only():
    raw = "```cpp\nint a = 5;\nstd::cout << a;\n```"
    clean, code, has_code = decision_service.preprocess_student_query(raw)
    assert clean == ""
    assert has_code is True
    assert "int a = 5;" in code


def test_preprocess_student_query_raw_unfenced_code():
    raw = "int a = 10;\nint* ptr = &a;\n*ptr = 20;\nstd::cout << a << std::endl;"
    clean, code, has_code = decision_service.preprocess_student_query(raw)
    assert clean == ""
    assert has_code is True
    assert "int* ptr = &a;" in code


def test_preprocess_student_query_text_only():
    raw = "Vòng lặp while hoạt động như thế nào trong C++?"
    clean, code, has_code = decision_service.preprocess_student_query(raw)
    assert clean == raw
    assert has_code is False
    assert code == ""


def test_preprocess_student_query_mixed():
    raw = "Đoạn code này bị lỗi gì vậy?\n```cpp\nint* p = nullptr;\n*p = 10;\n```"
    clean, code, has_code = decision_service.preprocess_student_query(raw)
    assert clean == "Đoạn code này bị lỗi gì vậy?"
    assert has_code is True
    assert "*p = 10;" in code


def test_homework_patterns_heuristic():
    hw_prompts = [
        "Làm hộ mình bài tập tính giai thừa",
        "Hãy viết code bài tập quản lý sinh viên giúp mình nộp bài",
        "Giải giúp em assignment 2 môn C++",
        "Cho mình xin code lab 3 với",
        "Làm dùm bài tập lớn",
    ]
    non_hw_prompts = [
        "Vòng lặp for và while khác nhau thế nào?",
        "Con trỏ và tham chiếu trong C++ có gì khác biệt?",
        "Giải thích cơ chế virtual function trong C++",
        "std::vector cấp phát bộ nhớ ra sao?",
    ]
    for prompt in hw_prompts:
        assert chat_service._HOMEWORK_PATTERNS.search(prompt) is not None, f"Failed to detect: {prompt}"

    for prompt in non_hw_prompts:
        assert chat_service._HOMEWORK_PATTERNS.search(prompt) is None, f"False positive: {prompt}"


def _fake_chunk():
    return rag_service.ScoredChunk(content="Sample content", score=0.85, metadata={})


def test_determine_action_has_code():
    action = chat_service._determine_chat_action(
        has_code=True,
        scope_decision=None,
        decision_chunks=[],
        is_high_zone=False,
        is_mid_zone=False,
        is_low_zone=True,
        is_hw=False,
    )
    assert action == "code_help"


def test_determine_action_gate_error_or_low_confidence():
    # High zone -> docs
    act_high = chat_service._determine_chat_action(
        has_code=False,
        scope_decision=None,
        decision_chunks=[_fake_chunk()],
        is_high_zone=True,
        is_mid_zone=False,
        is_low_zone=False,
        is_hw=False,
    )
    assert act_high == "docs"

    # Mid zone -> docs
    act_mid = chat_service._determine_chat_action(
        has_code=False,
        scope_decision=None,
        decision_chunks=[_fake_chunk()],
        is_high_zone=False,
        is_mid_zone=True,
        is_low_zone=False,
        is_hw=False,
    )
    assert act_mid == "docs"

    # Low zone -> safe_fallback (no web search allowed!)
    act_low = chat_service._determine_chat_action(
        has_code=False,
        scope_decision=None,
        decision_chunks=[],
        is_high_zone=False,
        is_mid_zone=False,
        is_low_zone=True,
        is_hw=False,
    )
    assert act_low == "safe_fallback"


def test_determine_action_comparison():
    comp_gate = decision_service.ScopeDecision(
        verdict=decision_service.ScopeVerdict.COMPARISON.value,
        confidence=0.9,
    )
    # High/Mid zone with chunks -> docs_plus_web
    act_docs_web = chat_service._determine_chat_action(
        has_code=False,
        scope_decision=comp_gate,
        decision_chunks=[_fake_chunk()],
        is_high_zone=True,
        is_mid_zone=False,
        is_low_zone=False,
        is_hw=False,
    )
    assert act_docs_web == "docs_plus_web"

    # Low zone with no chunks -> web_search
    act_web = chat_service._determine_chat_action(
        has_code=False,
        scope_decision=comp_gate,
        decision_chunks=[],
        is_high_zone=False,
        is_mid_zone=False,
        is_low_zone=True,
        is_hw=False,
    )
    assert act_web == "web_search"


def test_determine_action_extension():
    ext_gate = decision_service.ScopeDecision(
        verdict=decision_service.ScopeVerdict.EXTENSION.value,
        confidence=0.9,
    )
    core_gate = decision_service.ScopeDecision(
        verdict=decision_service.ScopeVerdict.CORE.value,
        confidence=0.9,
    )
    # High -> docs
    assert chat_service._determine_chat_action(
        has_code=False, scope_decision=ext_gate, decision_chunks=[_fake_chunk()],
        is_high_zone=True, is_mid_zone=False, is_low_zone=False, is_hw=False,
    ) == "docs"

    # Mid -> docs_plus_web
    assert chat_service._determine_chat_action(
        has_code=False, scope_decision=ext_gate, decision_chunks=[_fake_chunk()],
        is_high_zone=False, is_mid_zone=True, is_low_zone=False, is_hw=False,
    ) == "docs_plus_web"

    # Mid + homework -> docs_socratic
    assert chat_service._determine_chat_action(
        has_code=False, scope_decision=core_gate, decision_chunks=[_fake_chunk()],
        is_high_zone=False, is_mid_zone=True, is_low_zone=False, is_hw=True,
    ) == "docs_socratic"

    # Low + homework -> docs_socratic (NEVER web search for homework)
    assert chat_service._determine_chat_action(
        has_code=False, scope_decision=core_gate, decision_chunks=[],
        is_high_zone=False, is_mid_zone=False, is_low_zone=True, is_hw=True,
    ) == "docs_socratic"

    # Low -> web_search
    assert chat_service._determine_chat_action(
        has_code=False, scope_decision=ext_gate, decision_chunks=[],
        is_high_zone=False, is_mid_zone=False, is_low_zone=True, is_hw=False,
    ) == "web_search"


def test_determine_action_core():
    core_gate = decision_service.ScopeDecision(
        verdict=decision_service.ScopeVerdict.CORE.value,
        confidence=0.9,
    )
    # High -> docs
    assert chat_service._determine_chat_action(
        has_code=False, scope_decision=core_gate, decision_chunks=[_fake_chunk()],
        is_high_zone=True, is_mid_zone=False, is_low_zone=False, is_hw=False,
    ) == "docs"

    # High + homework -> docs_socratic
    assert chat_service._determine_chat_action(
        has_code=False, scope_decision=core_gate, decision_chunks=[_fake_chunk()],
        is_high_zone=True, is_mid_zone=False, is_low_zone=False, is_hw=True,
    ) == "docs_socratic"

    # Mid -> docs_plus_web
    assert chat_service._determine_chat_action(
        has_code=False, scope_decision=core_gate, decision_chunks=[_fake_chunk()],
        is_high_zone=False, is_mid_zone=True, is_low_zone=False, is_hw=False,
    ) == "docs_plus_web"

    # Low -> web_search
    assert chat_service._determine_chat_action(
        has_code=False, scope_decision=core_gate, decision_chunks=[],
        is_high_zone=False, is_mid_zone=False, is_low_zone=True, is_hw=False,
    ) == "web_search"
    # Low with roadmap -> docs (never web search)
    assert chat_service._determine_chat_action(
        has_code=False, scope_decision=core_gate, decision_chunks=[],
        is_high_zone=False, is_mid_zone=False, is_low_zone=True, is_hw=False,
        has_roadmap=True,
    ) == "docs"

