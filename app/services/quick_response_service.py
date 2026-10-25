"""Rule-based quick responses for greetings, identity, gratitude, and farewells.

Bypasses vector search and LLM generation to save tokens while keeping conversation natural.
"""

from __future__ import annotations

import random
import re
import unicodedata
from collections.abc import Sequence

# Greeting responses
GREETING_RESPONSES_VI = [
    "Xin chào! Mình là trợ lý AI học tập của môn học. Mình có thể giúp gì cho bạn về bài giảng, tài liệu hay bài tập hôm nay?",
    "Chào bạn! Rất vui được hỗ trợ bạn. Bạn cần tìm hiểu thông tin hay có câu hỏi nào về môn học này không?",
    "Xin chào! Chúc bạn một ngày học tập tốt lành. Hôm nay bạn cần mình hỗ trợ phần kiến thức nào?",
]

GREETING_RESPONSES_EN = [
    "Hello! I am your course AI assistant. What can I help you with regarding today's lectures or assignments?",
    "Hi there! Glad to assist you. Do you have any questions about the course materials?",
]

# Identity / Capability responses
IDENTITY_RESPONSES_VI = [
    (
        "Mình là trợ lý AI đồng hành cùng môn học của bạn. Mình có thể giúp bạn:\n\n"
        "• **Tra cứu tài liệu:** Tìm kiếm nhanh kiến thức trong các slide, giáo trình của lớp.\n"
        "• **Giải thích bài học:** Làm rõ các định nghĩa, thuật ngữ và khái niệm khó hiểu.\n"
        "• **Hỗ trợ học tập:** Hướng dẫn trả lời thắc mắc kèm theo trích dẫn nguồn tài liệu cụ thể.\n\n"
        "Bạn có thể đặt bất kỳ câu hỏi nào liên quan đến bài học để mình hỗ trợ nhé!"
    ),
]

IDENTITY_RESPONSES_EN = [
    (
        "I am your course AI learning assistant. I can help you with:\n\n"
        "• **Searching materials:** Quickly locate topics across class slides and syllabi.\n"
        "• **Explaining concepts:** Clarify terms, algorithms, and definitions.\n"
        "• **Answering questions:** Provide grounded answers with exact source citations.\n\n"
        "Feel free to ask any question about this course!"
    ),
]

# Gratitude responses
THANKS_RESPONSES_VI = [
    "Không có gì đâu bạn! Rất vui được hỗ trợ bạn. Chúc bạn học tập thật tốt nhé!",
    "Rất sẵn lòng! Nếu còn bất kỳ thắc mắc nào về bài học, bạn cứ hỏi mình nhé.",
    "Không có chi! Chúc bạn nắm vững kiến thức môn học này thật tốt.",
]

THANKS_RESPONSES_EN = [
    "You're very welcome! Happy studying, and feel free to ask if you have more questions.",
    "Glad I could help! Let me know whenever you need anything else.",
]

# Farewell responses
FAREWELL_RESPONSES_VI = [
    "Tạm biệt bạn! Chúc bạn học tập và làm việc hiệu quả nhé!",
    "Hẹn gặp lại bạn! Cần tra cứu kiến thức môn học thì cứ quay lại nhắn mình nhé.",
]

FAREWELL_RESPONSES_EN = [
    "Goodbye! Wishing you great success with your studies!",
    "See you later! Feel free to return anytime you need help with the course.",
]

# Unaccented regex patterns for resilient matching
GREETING_PATTERNS = [
    r"^xin\s*chao(?:\s+(?:ban|bot|ad|admin|minh|cau))?$",
    r"^chao(?:\s+(?:ban|bot|ad|admin|minh|cau|buoi\s*sang|buoi\s*chieu|buoi\s*toi))?$",
    r"^he\s*lo(?:\s+(?:ban|bot|ad))?$",
    r"^alo(?:\s+(?:ban|bot|ad|admin))?$",
    r"^hi(?:\s+(?:there|bot|everyone|ban|ad))?$",
    r"^hello(?:\s+(?:there|bot|everyone|ban|ad))?$",
    r"^hey(?:\s+(?:there|bot))?$",
    r"^good\s+(?:morning|afternoon|evening|day)$",
    r"^howdy$",
]

IDENTITY_PATTERNS = [
    r"^ban\s+la\s+(?:ai|gi)$",
    r"^may\s+la\s+(?:ai|gi)$",
    r"^bot\s+la\s+(?:ai|gi)$",
    r"^he\s*thong\s+nay\s+la\s+(?:gi|ai)$",
    r"^(?:ban|bot)\s+(?:co\s+the|lam\s+duoc)\s+(?:lam\s+)?gi$",
    r"^chuc\s+nang\s+(?:cua\s+ban|cua\s+bot)$",
    r"^gioi\s+thieu\s+(?:ban\s+than|ve\s+ban|ve\s+bot)$",
    r"^huong\s+dan(?:\s+su\s+dung)?$",
    r"^tro\s+giup$",
    r"^who\s+are\s+you$",
    r"^what\s+are\s+you$",
    r"^what\s+can\s+you\s+do$",
    r"^tell\s+me\s+about\s+yourself$",
    r"^help$",
    r"^how\s+does\s+this\s+work$",
]

THANKS_PATTERNS = [
    r"^(?:ok\s+|oke\s+|oki\s+)?cam\s*on(?:\s+(?:ban|bot|nhieu|nha|nhe))?$",
    r"^thank(?:\s+you|\s+bot)?$",
    r"^thanks(?:\s+a\s+lot)?$",
    r"^thank\s+you(?:\s+(?:very\s+much|so\s+much))?$",
    r"^(?:tks|thx)(?:\s+(?:ban|bot))?$",
]

FAREWELL_PATTERNS = [
    r"^tam\s+biet(?:\s+(?:ban|bot|nhe))?$",
    r"^(?:chao\s+)?tam\s+biet$",
    r"^hen\s+gap\s+lai$",
    r"^bye(?:\s+bye|\s+bot)?$",
    r"^goodbye$",
    r"^see\s+you(?:\s+later)?$",
    r"^see\s+ya$",
]


def strip_accents(text: str) -> str:
    """Normalize and strip Vietnamese / Unicode diacritics."""
    text = text.replace("đ", "d").replace("Đ", "d")
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _normalize(text: str) -> str:
    cleaned = text.strip().lower()
    # Remove trailing punctuation like '.', '!', '?', '~'
    cleaned = re.sub(r"[.!?,~]+$", "", cleaned).strip()
    # Normalize multiple whitespace characters
    cleaned = re.sub(r"\s+", " ", cleaned)
    return strip_accents(cleaned)


def _is_english_query(text: str) -> bool:
    en_markers = {"hi", "hello", "hey", "who", "what", "how", "thanks", "thank", "bye", "goodbye", "help"}
    words = set(text.lower().split())
    return bool(words.intersection(en_markers))


def _matches_any(text: str, patterns: Sequence[str]) -> bool:
    return any(re.match(pat, text, re.IGNORECASE) for pat in patterns)


def get_quick_response(message: str) -> str | None:
    """Return a hardcoded response for chit-chat / system queries, or None.

    Bypasses RAG if the query matches greetings, capability checks, gratitude, or goodbyes.
    Fails closed (returns None) if message looks like a real course question.
    """
    normalized = _normalize(message)
    if not normalized:
        return None

    # Guard: Don't intercept questions that are longer or look like real inquiries
    # e.g., "Chào bạn, cho mình hỏi về bài tập 1" should pass to RAG
    word_count = len(normalized.split())
    if word_count > 10:
        return None

    is_en = _is_english_query(message)

    if _matches_any(normalized, GREETING_PATTERNS):
        return random.choice(GREETING_RESPONSES_EN if is_en else GREETING_RESPONSES_VI)
    if _matches_any(normalized, IDENTITY_PATTERNS):
        return random.choice(IDENTITY_RESPONSES_EN if is_en else IDENTITY_RESPONSES_VI)
    if _matches_any(normalized, THANKS_PATTERNS):
        return random.choice(THANKS_RESPONSES_EN if is_en else THANKS_RESPONSES_VI)
    if _matches_any(normalized, FAREWELL_PATTERNS):
        return random.choice(FAREWELL_RESPONSES_EN if is_en else FAREWELL_RESPONSES_VI)

    return None


def split_into_stream_chunks(text: str, chunk_size: int = 4) -> list[str]:
    """Split text into small word bundles to simulate a natural typing stream."""
    words = text.split(" ")
    chunks: list[str] = []
    for i in range(0, len(words), chunk_size):
        group = words[i : i + chunk_size]
        chunk_text = " ".join(group)
        if i + chunk_size < len(words):
            chunk_text += " "
        chunks.append(chunk_text)
    return chunks
