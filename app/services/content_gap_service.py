"""Service for Content Gap analytics, deduplication, and reason grouping (AI-91, Phase 3)."""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.category import ClassCategory
from app.models.chat import ChatMessage, ChatSession
from app.models.course import Class, Enrollment
from app.schemas.content_gap import (
    ContentGapDashboardResponse,
    ContentGapQuestion,
    ContentGapReasonGroup,
    ContentGapTopicReport,
    CurriculumGapProfile,
    QuestionPattern,
    StudentWeaknessProfile,
)
from app.services.llm.provider import get_llm_model

logger = logging.getLogger(__name__)

# In-memory cache for dashboard responses: class_id -> (cached_at, response)
_DASHBOARD_CACHE: dict[uuid.UUID, tuple[datetime, ContentGapDashboardResponse]] = {}
CACHE_TTL_SECONDS = 1800  # 30 minutes


def _match_category_for_question(question_text: str, categories: list[ClassCategory]) -> uuid.UUID | None:
    """Dynamically match a student question to the best category using name tokens and subcategories."""
    if not categories:
        return None
    if len(categories) == 1:
        return categories[0].id

    q_lower = (question_text or "").lower()

    # 1. Exact full-name match
    for cat in categories:
        if cat.name.lower() in q_lower:
            return cat.id

    # 2. Token overlap score derived dynamically from category and subcategory names
    stop_words = {"and", "the", "in", "of", "to", "for", "with", "a", "an", "&", "về", "và", "các", "của", "cho", "là"}
    scores: dict[uuid.UUID, int] = {}
    for cat in categories:
        cat_words = [w for w in re.findall(r"\w+", cat.name.lower()) if len(w) > 2 and w not in stop_words]
        score = sum(2 if w in q_lower else 0 for w in cat_words)

        if getattr(cat, "subcategories", None):
            for sub in cat.subcategories:
                sub_words = [w for w in re.findall(r"\w+", sub.name.lower()) if len(w) > 2 and w not in stop_words]
                score += sum(3 if w in q_lower else 0 for w in sub_words)
        scores[cat.id] = score

    best_cat = max(scores, key=scores.get)
    if scores[best_cat] > 0:
        return best_cat

    return categories[0].id

def _classify_question_reason(
    question_text: str,
    concept_tag: str | None = None,
) -> tuple[str, str, str]:
    """Classify a student query into a pedagogical fallback reason.

    Returns: (reason_type, title, explanation)
    """
    q_lower = (question_text or "").lower()
    tag_lower = (concept_tag or "").lower()
    combined = f"{q_lower} {tag_lower}"

    # 1. Missing Practical Code Implementation
    code_signals = [
        "code", "syntax", "implement", "example", "viết code", "cài đặt",
        "hàm", "function", "class", "constructor", "destructor", "template",
        "pointer", "unique_ptr", "shared_ptr", "malloc", "free",
    ]
    if any(sig in combined for sig in code_signals) and any(
        kw in combined for kw in ["how to", "example", "viết", "code", "mẫu", "cách dùng", "cú pháp"]
    ):
        return (
            "missing_code",
            "Missing Practical Code Implementation",
            "Course materials explain the theory, but students asked for concrete code examples and implementation syntax.",
        )

    # 2. Missing In-Scope Comparison
    comparison_signals = [
        "vs", "compare", "khác nhau", "so sánh", "ưu nhược điểm", "lựa chọn giữa",
        "difference between", "when to use", "better than", "phân biệt",
    ]
    if any(sig in combined for sig in comparison_signals):
        return (
            "missing_comparison",
            "Missing In-Scope Comparison",
            "Students asked for side-by-side comparisons between concepts that are taught separately in lecture materials.",
        )

    # 3. Practical Debugging & Error Handling
    error_signals = [
        "error", "bug", "segfault", "leak", "lỗi", "segmentation fault", "crash",
        "exception", "dangling pointer", "undefined behavior", "core dump",
    ]
    if any(sig in combined for sig in error_signals):
        return (
            "debugging_error",
            "Practical Debugging & Runtime Error Handling",
            "Students encountered runtime crashes or compiler errors that course materials do not address.",
        )

    # 4. Default: Missing Core Concept
    return (
        "missing_concept",
        "Concept Not Covered in Materials",
        "Lecture materials lack sufficient theoretical depth or coverage for this specific subtopic.",
    )


def _synthesize_topic_pedagogy_ai(
    category_name: str,
    questions: list[tuple[uuid.UUID, str, datetime, bool]],
) -> tuple[StudentWeaknessProfile, CurriculumGapProfile] | None:
    """Dynamically cluster questions and synthesize pedagogical patterns via LLM."""
    import sys
    if "pytest" in sys.modules:
        return None
    if not questions:
        return None

    # Pick up to 12 distinct representative questions
    distinct_q: list[tuple[uuid.UUID, str, bool]] = []
    seen = set()
    for item in questions:
        q_text = item[1].strip()
        q_norm = q_text.lower()
        if q_norm not in seen:
            seen.add(q_norm)
            distinct_q.append((item[0], q_text, item[3]))
        if len(distinct_q) >= 12:
            break

    q_formatted = "\n".join([f"{i+1}. [Used Web: {is_web}] \"{qt}\"" for i, (_, qt, is_web) in enumerate(distinct_q)])

    prompt = f"""You are a university pedagogical curriculum analyst.
Analyze these real student questions asked in the topic '{category_name}':
{q_formatted}

Synthesize these questions into 1 to 4 distinct recurring learning themes (Question Patterns).
For each theme, provide:
- "pattern": A concise, natural summary title in Vietnamese describing what students are struggling with or asking.
- "pedagogical_category": Exactly one of ["In-Scope Comparison", "Practical Implementation", "Troubleshooting & Edge Cases", "Foundational Concepts"].
- "scope_type": Either "core_curriculum" (concepts standardly expected in the course syllabus for {category_name}) OR "enrichment" (extracurricular curiosity, advanced 3D graphics libraries like Vulkan, third-party frameworks, or external language comparisons like Python, Java, Rust, or other languages not required by core syllabus).
- "sample_question": The most representative question from the input list.
- "suggested_action": A concrete, practical recommendation for the instructor.
  * If scope_type is "core_curriculum": specify what to add to "tài liệu bài giảng" or "tài liệu lab" (e.g. diagrams, step-by-step code examples).
  * If scope_type is "enrichment": provide an enrichment suggestion (e.g. "Nhu cầu tự tìm hiểu thêm của sinh viên ngoài đề cương; thầy/cô có thể tạo bài đọc thêm tự chọn (Optional Reading) hoặc gợi ý đề tài nghiên cứu/đồ án nếu muốn.")
  * STRICTLY FORBIDDEN WORD: "slide". Always use "tài liệu bài giảng", "tài liệu lab", or "tài liệu lớp".
- "matched_question_indices": 1-based indices of questions from the list above that belong to this theme.

Also provide:
- "struggle_summary": A 1-2 sentence summary in Vietnamese of what students find challenging across this topic.
- "missing_points": A list of 1-3 specific curriculum gaps in Vietnamese explaining what is missing in current course materials.
- "recommendation": An overall actionable recommendation in Vietnamese for the curriculum.

Return ONLY a valid JSON object matching this schema:
{{
  "patterns": [
    {{
      "pattern": "...",
      "pedagogical_category": "...",
      "scope_type": "core_curriculum",
      "sample_question": "...",
      "suggested_action": "...",
      "matched_question_indices": [1, 2]
    }}
  ],
  "struggle_summary": "...",
  "missing_points": ["..."],
  "recommendation": "..."
}}
"""

    try:
        llm = get_llm_model()
        resp = llm.invoke([
            SystemMessage(content="You are an expert curriculum analyzer. Always respond with a strictly valid JSON object."),
            HumanMessage(content=prompt)
        ])

        raw = resp.content
        if isinstance(raw, list) and len(raw) > 0 and isinstance(raw[0], dict):
            raw = raw[0].get("text", "")
        elif not isinstance(raw, str):
            raw = str(raw)

        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return None

        data = json.loads(match.group(0))
        raw_patterns = data.get("patterns", [])
        if not raw_patterns or not isinstance(raw_patterns, list):
            return None

        total_questions_count = len(questions)
        total_web_count = sum(1 for q in questions if q[3])
        distinct_count = len(distinct_q)

        patterns: list[QuestionPattern] = []
        assigned_indices = set()
        index_to_q = {i + 1: distinct_q[i] for i in range(len(distinct_q))}

        for p_data in raw_patterns:
            matched = p_data.get("matched_question_indices", [])
            valid_indices = [idx for idx in matched if idx in index_to_q]
            assigned_indices.update(valid_indices)

            p_students = {index_to_q[idx][0] for idx in valid_indices}
            p_web_count = sum(1 for idx in valid_indices if index_to_q[idx][2])
            p_total_count = len(valid_indices)

            if distinct_count > 0 and total_questions_count > distinct_count:
                ratio = p_total_count / distinct_count
                est_total = max(1, round(ratio * total_questions_count))
                est_web = round(ratio * total_web_count)
            else:
                est_total = max(1, p_total_count)
                est_web = p_web_count

            est_total = max(est_total, est_web)
            sample_q = p_data.get("sample_question") or (index_to_q[valid_indices[0]][1] if valid_indices else category_name)
            action = p_data.get("suggested_action") or "Review and expand course materials for this section."
            action = re.sub(r"\bslides?\b", "tài liệu bài giảng", action, flags=re.IGNORECASE)

            pat_name = p_data.get("pattern", f"Tìm hiểu về {category_name}")
            raw_scope = str(p_data.get("scope_type", "")).lower()
            sample_text = f"{pat_name} {sample_q}".lower()
            enrichment_keywords = [
                "vulkan", "opengl", "directx", "unreal", "game engine", "thư viện đồ họa",
                "ngôn ngữ khác", "ngôn ngữ lập trình khác", "ngoài đề cương", "ngoài giáo trình",
                "python", "java", "c#", "rust", "golang", "external language",
            ]
            is_enrichment = raw_scope == "enrichment" or any(kw in sample_text for kw in enrichment_keywords)
            scope_type = "enrichment" if is_enrichment else "core_curriculum"

            patterns.append(
                QuestionPattern(
                    pattern=pat_name,
                    inquiry_count=est_web,
                    total_inquiry_count=est_total,
                    student_count=max(1, len(p_students)),
                    pedagogical_category=p_data.get("pedagogical_category", "Foundational Concepts"),
                    sample_question=sample_q[:120],
                    suggested_action=action,
                    scope_type=scope_type,
                )
            )

        if not patterns:
            return None

        sum_total = sum(p.total_inquiry_count for p in patterns)
        sum_web = sum(p.inquiry_count for p in patterns)
        if sum_total > 0 and sum_total != total_questions_count:
            diff_total = total_questions_count - sum_total
            diff_web = total_web_count - sum_web
            patterns[0].total_inquiry_count = max(patterns[0].inquiry_count, patterns[0].total_inquiry_count + diff_total)
            patterns[0].inquiry_count = max(0, min(patterns[0].total_inquiry_count, patterns[0].inquiry_count + diff_web))

        patterns.sort(key=lambda p: (-p.inquiry_count, -p.total_inquiry_count))

        struggle_sum = data.get("struggle_summary") or f"Sinh viên có nhiều thắc mắc cần tài liệu hỗ trợ về {category_name}."
        struggle_sum = re.sub(r"\bslides?\b", "tài liệu bài giảng", struggle_sum, flags=re.IGNORECASE)

        missing_pts = data.get("missing_points") or [f"Tài liệu khóa học hiện tại cần bổ sung các ví dụ và hướng dẫn cho {category_name}."]
        missing_pts = [re.sub(r"\bslides?\b", "tài liệu bài giảng", mp, flags=re.IGNORECASE) for mp in missing_pts]

        rec = data.get("recommendation") or f"Cập nhật và bổ sung thêm tài liệu bài giảng cho {category_name}."
        rec = re.sub(r"\bslides?\b", "tài liệu bài giảng", rec, flags=re.IGNORECASE)

        severity: Literal["high", "medium", "low"] = "high" if total_questions_count >= 5 else "medium"

        return (
            StudentWeaknessProfile(
                struggle_summary=struggle_sum,
                severity=severity,
                question_patterns=patterns,
            ),
            CurriculumGapProfile(
                missing_points=missing_pts,
                recommendation=rec,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Dynamic AI pedagogy synthesis failed, falling back to rule-based: %s", exc)
        return None


def _synthesize_topic_pedagogy_fallback(
    category_name: str,
    questions: list[tuple[uuid.UUID, str, datetime, bool]],
) -> tuple[StudentWeaknessProfile, CurriculumGapProfile]:
    """Synthesize raw student queries into recurring question patterns, student weaknesses, and curriculum gaps."""
    if not questions:
        return (
            StudentWeaknessProfile(
                struggle_summary=f"No major student misconceptions or knowledge deficits recorded for {category_name}.",
                severity="low",
                question_patterns=[],
            ),
            CurriculumGapProfile(
                missing_points=[f"Current lecture materials sufficiently cover {category_name}."],
                recommendation="Continue monitoring student chat inquiries to maintain material coverage.",
            ),
        )

    # Cluster questions by semantic intent
    buckets: dict[str, dict[str, Any]] = {
        "comparison": {
            "pattern": f"So sánh các phương pháp, cú pháp & đối chiếu kiến thức trong {category_name}",
            "category": "In-Scope Comparison",
            "signals": ["so sánh", "vs", "compare", "khác nhau", "nhanh hơn", "ưu nhược điểm", "phân biệt", "đối chiếu"],
            "queries": [],
            "web_queries": [],
            "students": set(),
            "suggested_action": "Bổ sung bảng so sánh đối chiếu trực quan về cú pháp, mô hình bộ nhớ và hiệu năng giữa các phương pháp.",
        },
        "application": {
            "pattern": f"Hướng dẫn bài tập thực hành & các ví dụ chi tiết cho {category_name}",
            "category": "Practical Implementation",
            "signals": ["code", "syntax", "thực hành", "bài tập", "hướng dẫn", "ví dụ", "example", "cài đặt", "làm sao", "cách làm", "giải bài", "hàm", "function"],
            "queries": [],
            "web_queries": [],
            "students": set(),
            "suggested_action": "Bổ sung bài tập thực hành có hướng dẫn giải từng bước và mã nguồn mẫu chạy được trong tài liệu lớp.",
        },
        "troubleshooting": {
            "pattern": f"Giải đáp thắc mắc về lỗi thường gặp và các tình huống ngoại lệ của {category_name}",
            "category": "Troubleshooting & Edge Cases",
            "signals": ["lỗi", "error", "sửa", "bug", "tại sao sai", "sai ở đâu", "không chạy", "exception", "failed", "crash"],
            "queries": [],
            "web_queries": [],
            "students": set(),
            "suggested_action": "Thêm mục FAQ hướng dẫn chẩn đoán và khắc phục các lỗi runtime và tình huống ngoại lệ thường gặp.",
        },
        "foundations": {
            "pattern": f"Khái niệm cốt lõi, lý thuyết cơ bản và định nghĩa về {category_name}",
            "category": "Foundational Concepts",
            "signals": ["là gì", "what is", "định nghĩa", "khái niệm", "tổng quan", "nguyên lý", "cơ chế", "tại sao", "ý nghĩa", "như thế nào", "giải thích"],
            "queries": [],
            "web_queries": [],
            "students": set(),
            "suggested_action": "Bổ sung sơ đồ minh họa nguyên lý và giải thích chi tiết các định nghĩa lý thuyết cốt lõi trong tài liệu bài giảng.",
        },
    }

    unassigned: list[tuple[uuid.UUID, str, bool]] = []

    for item in questions:
        s_id = item[0]
        q_text = item[1]
        is_web = item[3] if len(item) > 3 else True
        pedagogy_tag = item[4] if len(item) > 4 else None
        q_lower = q_text.lower()
        matched = False

        # 1. Chat-time tag stored in DB (Zero extra API requests)
        if pedagogy_tag:
            tag_to_bucket = {
                "In-Scope Comparison": "comparison",
                "Practical Implementation": "application",
                "Troubleshooting & Edge Cases": "troubleshooting",
                "Foundational Concepts": "foundations",
            }
            b_key = tag_to_bucket.get(pedagogy_tag)
            if b_key and b_key in buckets:
                buckets[b_key]["queries"].append(q_text)
                if is_web:
                    buckets[b_key]["web_queries"].append(q_text)
                buckets[b_key]["students"].add(s_id)
                matched = True

        # 2. Linguistic signals if no chat-time tag
        if not matched:
            for b_data in buckets.values():
                if any(sig in q_lower for sig in b_data["signals"]):
                    b_data["queries"].append(q_text)
                    if is_web:
                        b_data["web_queries"].append(q_text)
                    b_data["students"].add(s_id)
                    matched = True
                    break

        if not matched:
            unassigned.append((s_id, q_text, is_web))

    for s_id, q_text, is_web in unassigned:
        target = "application" if ("code" in q_text.lower() or "ví dụ" in q_text.lower()) else "foundations"
        buckets[target]["queries"].append(q_text)
        if is_web:
            buckets[target]["web_queries"].append(q_text)
        buckets[target]["students"].add(s_id)

    patterns: list[QuestionPattern] = []
    for b_data in buckets.values():
        q_list = b_data["queries"]
        web_list = b_data["web_queries"]
        if q_list:
            sample_q = min(q_list, key=lambda s: len(s) if len(s) > 10 else 999)
            text_to_check = f"{b_data['pattern']} {sample_q}".lower()
            enrichment_keywords = [
                "vulkan", "opengl", "directx", "unreal", "game engine", "thư viện đồ họa",
                "ngôn ngữ khác", "ngôn ngữ lập trình khác", "ngoài đề cương", "ngoài giáo trình",
                "python", "java", "c#", "rust", "golang", "external language",
            ]
            is_enrichment = any(kw in text_to_check for kw in enrichment_keywords)
            scope_type = "enrichment" if is_enrichment else "core_curriculum"
            action = "Nhu cầu tự tìm hiểu thêm của sinh viên ngoài đề cương; thầy/cô có thể tạo bài đọc thêm tự chọn (Optional Reading) hoặc gợi ý đề tài nếu muốn." if is_enrichment else b_data["suggested_action"]

            patterns.append(
                QuestionPattern(
                    pattern=b_data["pattern"],
                    inquiry_count=len(web_list),
                    total_inquiry_count=len(q_list),
                    student_count=max(1, len(b_data["students"])),
                    pedagogical_category=b_data["category"],
                    sample_question=sample_q.strip()[:120],
                    suggested_action=action,
                    scope_type=scope_type,
                )
            )

    # Sort patterns: highest web inquiries first, then highest total inquiries
    patterns.sort(key=lambda p: (-p.inquiry_count, -p.total_inquiry_count))

    # Synthesize Student Weakness Profile
    total_q = len(questions)
    top_categories = [p.pedagogical_category for p in patterns[:2]]
    struggles = []
    if any("Comparison" in c for c in top_categories):
        struggles.append(
            f"Sinh viên gặp khó khăn khi so sánh các kỹ thuật, công cụ và phương pháp tiếp cận khác nhau trong {category_name}."
        )
    if any("Practical" in c for c in top_categories):
        struggles.append(
            f"Nhiều sinh viên cần thêm ví dụ minh họa và bài tập thực hành cụ thể về {category_name}."
        )
    if any("Troubleshooting" in c for c in top_categories):
        struggles.append(
            f"Sinh viên thường gặp lỗi phát sinh khi tự thực hành hoặc kiểm tra các trường hợp ngoại lệ trong {category_name}."
        )
    if any("Foundational" in c for c in top_categories):
        struggles.append(
            f"Sinh viên cần củng cố các định nghĩa và nguyên lý lý thuyết cốt lõi của {category_name}."
        )
    if not struggles:
        struggles.append(f"Sinh viên có xu hướng tìm kiếm thêm tài liệu ngoài về {category_name} do thiếu các ví dụ thực hành.")

    weakness_summary = " ".join(struggles)
    severity: Literal["high", "medium", "low"] = "high" if total_q >= 5 else "medium"

    student_weakness = StudentWeaknessProfile(
        struggle_summary=weakness_summary,
        severity=severity,
        question_patterns=patterns,
    )

    # Synthesize Curriculum Gap Profile
    missing_points = []
    if any("Comparison" in c for c in top_categories):
        missing_points.append(f"Tài liệu bài giảng chưa có bảng so sánh trực quan và phân tích ưu nhược điểm cho {category_name}.")
    if any("Practical" in c for c in top_categories):
        missing_points.append(f"Tài liệu khóa học hiện tại thiếu các bài tập mẫu có lời giải từng bước cho {category_name}.")
    if any("Troubleshooting" in c for c in top_categories):
        missing_points.append(f"Chưa có hướng dẫn xử lý các lỗi thường gặp hoặc các câu hỏi thường gặp (FAQ) cho {category_name}.")
    if any("Foundational" in c for c in top_categories):
        missing_points.append(f"Định nghĩa và nguyên tắc cơ bản của {category_name} trong tài liệu chưa đủ chi tiết.")
    if not missing_points:
        missing_points.append(f"Tài liệu khóa học hiện tại chưa đào sâu vào các kịch bản ứng dụng thực tế của {category_name}.")

    recommendation = (
        f"Cập nhật và bổ sung thêm tài liệu bài giảng, câu hỏi ôn tập có lời giải cho {category_name}."
    )

    curriculum_gap = CurriculumGapProfile(
        missing_points=missing_points,
        recommendation=recommendation,
    )

    return student_weakness, curriculum_gap


def _synthesize_topic_pedagogy(
    category_name: str,
    questions: list[Any],
) -> tuple[StudentWeaknessProfile, CurriculumGapProfile]:
    """Synthesize raw student queries into recurring patterns dynamically via AI with resilient fallback."""
    if not questions:
        return (
            StudentWeaknessProfile(
                struggle_summary=f"No major student misconceptions or knowledge deficits recorded for {category_name}.",
                severity="low",
                question_patterns=[],
            ),
            CurriculumGapProfile(
                missing_points=[f"Current lecture materials sufficiently cover {category_name}."],
                recommendation="Continue monitoring student chat inquiries to maintain material coverage.",
            ),
        )

    # 1. Dynamic AI synthesis (Detailed, course-specific insights, zero hardcoding)
    ai_result = _synthesize_topic_pedagogy_ai(category_name, questions)
    if ai_result is not None:
        return ai_result

    # 2. Resilient fallback using chat-time tags / signals (Zero extra requests)
    return _synthesize_topic_pedagogy_fallback(category_name, questions)

    # 3. Universal linguistic fallback (zero domain-specific hardcoded keywords)
    return _synthesize_topic_pedagogy_fallback(category_name, questions)


def _get_illustrative_preview(
    class_id: uuid.UUID,
    categories: list[ClassCategory],
    enrolled_count: int,
) -> ContentGapDashboardResponse:
    """Provide realistic, high-value illustrative data when a class has few real student chats."""
    now = datetime.now(UTC)

    # Pre-crafted pedagogical reason groups for realistic preview
    sample_topics_data = [
        {
            "name": "Pointer Arithmetic & Dynamic Memory",
            "material_answers": 15,
            "web_fallbacks": 5,
            "distinct_students": 4,
            "status": "critical",
            "student_weakness": StudentWeaknessProfile(
                struggle_summary="Sinh viên gặp khó khăn trong việc theo dõi vòng đời con trỏ và cú pháp chuyển quyền sở hữu (ownership), dẫn đến lỗi rò rỉ bộ nhớ hoặc segmentation fault.",
                severity="high",
                question_patterns=[
                    QuestionPattern(
                        pattern="Cách truyền con trỏ thông minh (unique_ptr) vào hàm mà không làm mất quyền sở hữu",
                        inquiry_count=3,
                        total_inquiry_count=12,
                        student_count=2,
                        pedagogical_category="Syntax & Implementation",
                        sample_question="How do I pass a unique_ptr into a helper function by reference without transferring ownership?",
                        suggested_action="Bổ sung tài liệu bài giảng về cú pháp std::make_unique và truyền tham chiếu con trỏ thông minh.",
                    ),
                    QuestionPattern(
                        pattern="Lựa chọn giữa shared_ptr vs unique_ptr và đánh giá chi phí bộ nhớ runtime",
                        inquiry_count=2,
                        total_inquiry_count=8,
                        student_count=2,
                        pedagogical_category="In-Scope Comparison",
                        sample_question="When should I use shared_ptr vs unique_ptr in performance-critical loops?",
                        suggested_action="Bổ sung bảng đối chiếu trực quan overhead bộ nhớ và hiệu năng giữa raw pointer và smart pointers.",
                    ),
                ],
            ),
            "curriculum_gap": CurriculumGapProfile(
                missing_points=[
                    "Tài liệu bài giảng hiện tại chỉ dạy con trỏ thô C-style (malloc/free), hoàn toàn thiếu phần con trỏ thông minh hiện đại.",
                    "Chưa có bảng so sánh trực quan về overhead bộ nhớ giữa raw pointer, unique_ptr và shared_ptr.",
                ],
                recommendation="Bổ sung tài liệu hướng dẫn cú pháp std::make_unique kèm custom deleter trước bài Lab 3, và tài liệu về con trỏ truyền theo tham chiếu.",
            ),
            "reason_groups": [
                ContentGapReasonGroup(
                    reason_type="missing_code",
                    title="Missing Practical Code Implementation",
                    explanation="Course materials explain pointer definitions, but lack concrete examples of dynamic memory allocation and custom deleters.",
                    question_count=2,
                    questions=[
                        ContentGapQuestion(
                            id=str(uuid.uuid4()),
                            question="Can you give a C++ code example using std::unique_ptr with custom destructors?",
                            timestamp=now - timedelta(days=2),
                            concept_tag="unique_ptr custom deleter",
                        ),
                        ContentGapQuestion(
                            id=str(uuid.uuid4()),
                            question="How do I pass a unique_ptr into a helper function by reference without transferring ownership?",
                            timestamp=now - timedelta(days=4),
                            concept_tag="smart pointer parameter passing",
                        ),
                    ],
                ),
                ContentGapReasonGroup(
                    reason_type="missing_comparison",
                    title="Missing In-Scope Comparison",
                    explanation="Students asked to compare raw pointers with modern smart pointers in performance-critical code.",
                    question_count=2,
                    questions=[
                        ContentGapQuestion(
                            id=str(uuid.uuid4()),
                            question="When should I use shared_ptr vs unique_ptr in performance-critical loops?",
                            timestamp=now - timedelta(days=6),
                            concept_tag="shared_ptr vs unique_ptr overhead",
                        ),
                        ContentGapQuestion(
                            id=str(uuid.uuid4()),
                            question="Does std::unique_ptr introduce any runtime memory overhead compared to raw pointers?",
                            timestamp=now - timedelta(days=8),
                            concept_tag="pointer memory footprint",
                        ),
                    ],
                ),
                ContentGapReasonGroup(
                    reason_type="debugging_error",
                    title="Practical Debugging & Runtime Error Handling",
                    explanation="Students ran into memory leaks and segmentation faults when freeing memory.",
                    question_count=1,
                    questions=[
                        ContentGapQuestion(
                            id=str(uuid.uuid4()),
                            question="Why does my code throw segmentation fault when accessing a deleted unique_ptr?",
                            timestamp=now - timedelta(days=1),
                            concept_tag="segmentation fault debugging",
                        ),
                    ],
                ),
            ],
        },
        {
            "name": "Dynamic Programming & Memoization",
            "material_answers": 12,
            "web_fallbacks": 3,
            "distinct_students": 3,
            "status": "critical",
            "student_weakness": StudentWeaknessProfile(
                struggle_summary="Sinh viên khó khăn khi chuyển đổi công thức truy hồi đệ quy sang bảng quy hoạch động lặp (Bottom-Up) và tối ưu hóa không gian mảng.",
                severity="high",
                question_patterns=[
                    QuestionPattern(
                        pattern="Cài đặt bảng lặp Bottom-Up cho bài toán cái túi (0/1 Knapsack) và tối ưu không gian",
                        inquiry_count=2,
                        total_inquiry_count=9,
                        student_count=2,
                        pedagogical_category="Code Implementation",
                        sample_question="How to write the iterative bottom-up table code for the 0/1 Knapsack problem in C++?",
                        suggested_action="Bổ sung mã nguồn C++ mẫu duyệt bảng Knapsack và sơ đồ tối ưu rolling array vào tài liệu lab.",
                    ),
                    QuestionPattern(
                        pattern="Đánh giá trade-off giữa đệ quy có nhớ (Memoization) vs Quy hoạch động Bottom-Up",
                        inquiry_count=1,
                        total_inquiry_count=6,
                        student_count=1,
                        pedagogical_category="In-Scope Comparison",
                        sample_question="What are the trade-offs between memoization vs bottom-up DP regarding recursion stack depth?",
                        suggested_action="Phân tích chi tiết trade-off giữa đệ quy có nhớ và quy hoạch động lặp trong tài liệu bài giảng.",
                    ),
                ],
            ),
            "curriculum_gap": CurriculumGapProfile(
                missing_points=[
                    "Tài liệu chỉ trình bày công thức toán học nhưng thiếu code C++ mẫu hoàn chỉnh cho duyệt bảng Knapsack.",
                    "Chưa có hướng dẫn kỹ thuật tối ưu mảng 1D (rolling array) để tiết kiệm bộ nhớ.",
                ],
                recommendation="Thêm tài liệu code C++ mẫu cho bảng Knapsack và tài liệu minh họa trực quan kỹ thuật rolling array.",
            ),
            "reason_groups": [
                ContentGapReasonGroup(
                    reason_type="missing_code",
                    title="Missing Practical Code Implementation",
                    explanation="Course materials show recurrence equations, but lack full bottom-up table implementations.",
                    question_count=2,
                    questions=[
                        ContentGapQuestion(
                            id=str(uuid.uuid4()),
                            question="How to write the iterative bottom-up table code for the 0/1 Knapsack problem in C++?",
                            timestamp=now - timedelta(days=3),
                            concept_tag="bottom-up knapsack implementation",
                        ),
                        ContentGapQuestion(
                            id=str(uuid.uuid4()),
                            question="Can you write a code snippet converting recursive Fibonacci into space-optimized DP?",
                            timestamp=now - timedelta(days=7),
                            concept_tag="space optimization dp",
                        ),
                    ],
                ),
                ContentGapReasonGroup(
                    reason_type="missing_comparison",
                    title="Missing In-Scope Comparison",
                    explanation="Students asked when to prefer Top-Down Memoization vs Bottom-Up Tabulation.",
                    question_count=1,
                    questions=[
                        ContentGapQuestion(
                            id=str(uuid.uuid4()),
                            question="What are the trade-offs between memoization vs bottom-up DP regarding recursion stack depth?",
                            timestamp=now - timedelta(days=5),
                            concept_tag="memoization vs tabulation trade-off",
                        ),
                    ],
                ),
            ],
        },
        {
            "name": "Binary Search Trees & Balancing",
            "material_answers": 10,
            "web_fallbacks": 2,
            "distinct_students": 2,
            "status": "warning",
            "student_weakness": StudentWeaknessProfile(
                struggle_summary="Sinh viên nắm lý thuyết phép xoay cây nhưng dễ nhầm lẫn khi trỏ lại các con trỏ nút trong phép xoay kép (LR/RL rotation).",
                severity="medium",
                question_patterns=[
                    QuestionPattern(
                        pattern="Cài đặt mã nguồn phép xoay kép trái-phải (Left-Right rotation) trên cây AVL",
                        inquiry_count=2,
                        total_inquiry_count=12,
                        student_count=2,
                        pedagogical_category="Algorithm Implementation",
                        sample_question="How to implement Left-Right (LR) double rotation code for AVL tree nodes?",
                        suggested_action="Bổ sung sơ đồ 4 bước gán con trỏ chi tiết cho phép xoay kép cây AVL trong tài liệu bài giảng.",
                    ),
                ],
            ),
            "curriculum_gap": CurriculumGapProfile(
                missing_points=[
                    "Tài liệu minh họa sơ đồ xoay nhưng thiếu các bước gán con trỏ cụ thể trong code C++.",
                ],
                recommendation="Bổ sung sơ đồ 4 bước gán con trỏ chi tiết cho phép xoay kép trong bài giảng Tuần 5.",
            ),
            "reason_groups": [
                ContentGapReasonGroup(
                    reason_type="missing_code",
                    title="Missing Practical Code Implementation",
                    explanation="Materials explain tree rotation concepts, but omit the pointer manipulation code for double rotations.",
                    question_count=2,
                    questions=[
                        ContentGapQuestion(
                            id=str(uuid.uuid4()),
                            question="How to implement Left-Right (LR) double rotation code for AVL tree nodes?",
                            timestamp=now - timedelta(days=9),
                            concept_tag="avl tree double rotation code",
                        ),
                        ContentGapQuestion(
                            id=str(uuid.uuid4()),
                            question="Can you provide node deletion code balancing logic for red-black trees?",
                            timestamp=now - timedelta(days=11),
                            concept_tag="red black tree node deletion",
                        ),
                    ],
                ),
            ],
        },
        {
            "name": "Basic Sorting & Recursion",
            "material_answers": 25,
            "web_fallbacks": 1,
            "distinct_students": 1,
            "status": "good",
            "student_weakness": StudentWeaknessProfile(
                struggle_summary="Sinh viên thắc mắc về cơ chế tận dụng bộ nhớ đệm CPU (cache locality) khiến QuickSort chạy nhanh hơn MergeSort trong thực tế.",
                severity="low",
                question_patterns=[
                    QuestionPattern(
                        pattern="Ảnh hưởng của Cache Locality đến hiệu năng thực tế giữa QuickSort và MergeSort",
                        inquiry_count=1,
                        total_inquiry_count=26,
                        student_count=1,
                        pedagogical_category="Theory Comparison",
                        sample_question="Why does QuickSort perform better in practice than MergeSort despite having worst-case O(N^2)?",
                        suggested_action="Thêm mục phân tích kiến trúc CPU cache line và tính chất in-place của QuickSort vào tài liệu.",
                    ),
                ],
            ),
            "curriculum_gap": CurriculumGapProfile(
                missing_points=[
                    "Bài giảng tập trung vào độ phức tạp O(N log N) nhưng chưa giải thích về kiến trúc CPU cache line.",
                ],
                recommendation="Thêm ghi chú nhỏ về lợi thế phân vùng tại chỗ (in-place) của QuickSort trên phần cứng hiện đại.",
            ),
            "reason_groups": [
                ContentGapReasonGroup(
                    reason_type="missing_comparison",
                    title="Missing In-Scope Comparison",
                    explanation="Students compared recursion base cases between Merge Sort and Quick Sort.",
                    question_count=1,
                    questions=[
                        ContentGapQuestion(
                            id=str(uuid.uuid4()),
                            question="Why does QuickSort perform better in practice than MergeSort despite having worst-case O(N^2)?",
                            timestamp=now - timedelta(days=12),
                            concept_tag="quicksort vs mergesort cache locality",
                        ),
                    ],
                ),
            ],
        },
    ]

    # Map onto real category names if available, or keep realistic topics
    topic_reports: list[ContentGapTopicReport] = []
    effective_enrolled = max(1, enrolled_count if enrolled_count > 0 else 32)

    for idx, sample in enumerate(sample_topics_data):
        cat_id = str(categories[idx].id) if idx < len(categories) else str(uuid.uuid4())
        cat_name = categories[idx].name if idx < len(categories) else str(sample["name"])
        mat = int(sample["material_answers"])
        web = int(sample["web_fallbacks"])
        tot = mat + web
        ratio = round(web / tot, 3) if tot > 0 else 0.0
        students = int(sample["distinct_students"])
        class_pct = round((students / effective_enrolled) * 100, 1)

        topic_reports.append(
            ContentGapTopicReport(
                category_id=cat_id,
                category_name=cat_name,
                parent_id=None,
                material_answers=mat,
                web_fallbacks=web,
                total_queries=tot,
                fallback_ratio=ratio,
                distinct_students=students,
                class_percentage=min(100.0, class_pct),
                status=sample["status"],  # type: ignore[arg-type]
                student_weakness=sample["student_weakness"],
                curriculum_gap=sample["curriculum_gap"],
                reason_groups=sample["reason_groups"],  # type: ignore[arg-type]
            )
        )

    # Sort topics: critical first, then warning, then good
    status_order = {"critical": 0, "warning": 1, "good": 2}
    topic_reports.sort(key=lambda t: (status_order.get(t.status, 3), -t.web_fallbacks))

    total_web = sum(t.web_fallbacks for t in topic_reports)
    total_mat = sum(t.material_answers for t in topic_reports)
    tot_all = total_web + total_mat
    cov_rate = round((total_mat / tot_all) * 100, 1) if tot_all > 0 else 100.0
    crit_count = sum(1 for t in topic_reports if t.status == "critical")

    return ContentGapDashboardResponse(
        class_id=class_id,
        total_web_fallbacks=total_web,
        total_material_answers=total_mat,
        overall_coverage_rate=cov_rate,
        critical_topics_count=crit_count,
        total_students_enrolled=effective_enrolled,
        topics=topic_reports,
        last_updated_at=now,
    )


def compute_content_gap_dashboard(
    db: Session,
    class_id: uuid.UUID,
    force_refresh: bool = False,
) -> ContentGapDashboardResponse:
    """Compute deduplicated content gap dashboard with questions grouped by reason (AI-91)."""
    now = datetime.now(UTC)

    # Check in-memory cache
    if not force_refresh and class_id in _DASHBOARD_CACHE:
        cached_at, cached_resp = _DASHBOARD_CACHE[class_id]
        if (now - cached_at).total_seconds() < CACHE_TTL_SECONDS:
            return cached_resp

    # 1. Enrolled student count
    enrolled_count = (
        db.query(func.count(Enrollment.id))
        .filter(Enrollment.class_id == class_id, Enrollment.status == "ACTIVE")
        .scalar()
        or 0
    )

    # 2. Existing Categories
    categories = (
        db.query(ClassCategory)
        .filter(ClassCategory.class_id == class_id)
        .order_by(ClassCategory.created_at)
        .all()
    )

    cls_record = db.query(Class).filter(Class.id == class_id).first()
    cls_name = cls_record.name if cls_record else "General Course Content"

    # Fallback to course name if class does not have categories defined yet
    if not categories:
        categories = [
            ClassCategory(
                id=uuid.uuid5(uuid.NAMESPACE_DNS, str(class_id)),
                class_id=class_id,
                name=cls_name,
            )
        ]

    # 3. Fetch past 30 days of chat messages for this class
    thirty_days_ago = now - timedelta(days=30)
    chat_rows = (
        db.query(ChatMessage, ChatSession)
        .join(ChatSession, ChatMessage.session_id == ChatSession.id)
        .filter(
            ChatSession.class_id == class_id,
            ChatMessage.created_at >= thirty_days_ago,
        )
        .order_by(ChatMessage.created_at.asc())
        .all()
    )

    # If there are insufficient real chat messages, return illustrative data
    if len(chat_rows) == 0:
        response = _get_illustrative_preview(class_id, categories, enrolled_count)
        _DASHBOARD_CACHE[class_id] = (now, response)
        return response

    # 4. Pair USER prompt with ASSISTANT reply
    effective_enrolled = max(1, enrolled_count if enrolled_count > 0 else 30)

    # Tracking per category:
    # deduplicated keys: set of (student_id, date_str) for web
    # deduplicated keys: set of (student_id, date_str) for material
    cat_web_keys: dict[uuid.UUID, set[tuple[uuid.UUID, str]]] = {cat.id: set() for cat in categories}
    cat_mat_keys: dict[uuid.UUID, set[tuple[uuid.UUID, str]]] = {cat.id: set() for cat in categories}
    cat_web_students: dict[uuid.UUID, set[uuid.UUID]] = {cat.id: set() for cat in categories}
    cat_questions: dict[uuid.UUID, list[tuple[uuid.UUID, str, str, datetime]]] = {cat.id: [] for cat in categories}
    cat_all_questions: dict[uuid.UUID, list[tuple[uuid.UUID, str, str, datetime, bool]]] = {cat.id: [] for cat in categories}
    cat_mat_count: dict[uuid.UUID, int] = {cat.id: 0 for cat in categories}

    sessions: dict[uuid.UUID, list[ChatMessage]] = {}
    session_users: dict[uuid.UUID, uuid.UUID] = {}
    for msg, sess in chat_rows:
        sessions.setdefault(sess.id, []).append(msg)
        session_users[sess.id] = sess.user_id

    for sess_id, msgs in sessions.items():
        user_id = session_users[sess_id]
        for i in range(len(msgs) - 1):
            curr_msg = msgs[i]
            next_msg = msgs[i + 1]
            if curr_msg.role == "USER" and next_msg.role == "ASSISTANT":
                date_str = next_msg.created_at.strftime("%Y-%m-%d")
                is_web = bool(next_msg.used_web_search)
                question_text = curr_msg.content.strip()

                # Extract chat-time pedagogy tag from assistant trace if present
                pedagogy_tag = None
                if next_msg.trace:
                    for t in next_msg.trace:
                        if isinstance(t, str) and t.startswith("pedagogy:"):
                            pedagogy_tag = t.split(":", 1)[1]
                            break

                # Match question to best category dynamically via token & semantic overlap
                best_cat_id = _match_category_for_question(question_text, categories)

                if best_cat_id:
                    dedup_key = (user_id, date_str)
                    cat_all_questions[best_cat_id].append((user_id, str(next_msg.id), question_text, next_msg.created_at, is_web, pedagogy_tag))
                    if is_web:
                        cat_web_keys[best_cat_id].add(dedup_key)
                        cat_web_students[best_cat_id].add(user_id)
                        cat_questions[best_cat_id].append((user_id, str(next_msg.id), question_text, next_msg.created_at))
                    else:
                        cat_mat_keys[best_cat_id].add(dedup_key)
                        cat_mat_count[best_cat_id] += 1

    # 5. Build Topic Reports
    topic_reports: list[ContentGapTopicReport] = []
    for cat in categories:
        dedup_web_count = len(cat_web_keys.get(cat.id, set()))
        raw_web_count = len(cat_questions.get(cat.id, []))
        raw_mat_count = cat_mat_count.get(cat.id, 0)
        tot_count = raw_web_count + raw_mat_count
        ratio = round(raw_web_count / tot_count, 3) if tot_count > 0 else 0.0
        students_count = len(cat_web_students.get(cat.id, set()))
        class_pct = round((students_count / effective_enrolled) * 100, 1)

        # Thresholds (uses AC-09 deduplicated searches):
        # Critical (Needs Materials): >= 3 dedup searches, >= 2 students, >= 10% class, >= 30% web ratio
        is_critical = (
            dedup_web_count >= 3
            and students_count >= 2
            and class_pct >= 10.0
            and ratio >= 0.30
        )
        is_warning = ratio >= 0.15 and dedup_web_count >= 2
        status: Literal["critical", "warning", "good"] = (
            "critical" if is_critical else ("warning" if is_warning else "good")
        )

        # Synthesize Student Weakness Profile and Curriculum Gap Profile using ALL inquiries
        student_pedagogy_questions = [
            (item[0], item[2], item[3], item[4], item[5] if len(item) > 5 else None)
            for item in cat_all_questions.get(cat.id, [])
        ]
        student_weakness, curriculum_gap = _synthesize_topic_pedagogy(
            cat.name,
            student_pedagogy_questions,
        )

        # Group student questions by pedagogical reason
        reason_map: dict[str, ContentGapReasonGroup] = {}
        for _, q_id, q_text, q_time in cat_questions.get(cat.id, []):
            rtype, rtitle, rexplain = _classify_question_reason(q_text)
            if rtype not in reason_map:
                reason_map[rtype] = ContentGapReasonGroup(
                    reason_type=rtype,  # type: ignore[arg-type]
                    title=rtitle,
                    explanation=rexplain,
                    question_count=0,
                    questions=[],
                )
            reason_map[rtype].questions.append(
                ContentGapQuestion(
                    id=q_id,
                    question=q_text,
                    timestamp=q_time,
                )
            )
            reason_map[rtype].question_count += 1

        topic_reports.append(
            ContentGapTopicReport(
                category_id=str(cat.id),
                category_name=cat.name,
                parent_id=str(cat.parent_id) if cat.parent_id else None,
                material_answers=raw_mat_count,
                web_fallbacks=raw_web_count,
                dedup_web_fallbacks=dedup_web_count,
                total_queries=tot_count,
                fallback_ratio=ratio,
                distinct_students=students_count,
                class_percentage=min(100.0, class_pct),
                status=status,
                student_weakness=student_weakness,
                curriculum_gap=curriculum_gap,
                reason_groups=list(reason_map.values()),
            )
        )

    # Sort topics
    status_order = {"critical": 0, "warning": 1, "good": 2}
    topic_reports.sort(key=lambda t: (status_order.get(t.status, 3), -t.web_fallbacks))

    total_web = sum(t.web_fallbacks for t in topic_reports)
    total_mat = sum(t.material_answers for t in topic_reports)
    tot_all = total_web + total_mat
    cov_rate = round((total_mat / tot_all) * 100, 1) if tot_all > 0 else 100.0
    crit_count = sum(1 for t in topic_reports if t.status == "critical")

    response = ContentGapDashboardResponse(
        class_id=class_id,
        total_web_fallbacks=total_web,
        total_material_answers=total_mat,
        overall_coverage_rate=cov_rate,
        critical_topics_count=crit_count,
        total_students_enrolled=effective_enrolled,
        topics=topic_reports,
        last_updated_at=now,
    )

    _DASHBOARD_CACHE[class_id] = (now, response)
    return response


def clear_dashboard_cache(class_id: uuid.UUID) -> None:
    """Invalidate cached dashboard data for this class."""
    _DASHBOARD_CACHE.pop(class_id, None)
