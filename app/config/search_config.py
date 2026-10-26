"""Configuration for Internet Web Search (Tavily).

Defines trusted, high-reputation domain tiers (official documentation,
academic institutions, curated references) and blocked spam/cheat sites
to ensure high quality academic citations.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Tier 1: Nguồn chính thức (tài liệu gốc, trường đại học, tổ chức chuẩn)
# ---------------------------------------------------------------------------
TIER1_OFFICIAL_DOMAINS: list[str] = [
    # --- Chuẩn chương trình & tổ chức học thuật ---
    "acm.org",
    "ieee.org",
    "abet.org",
    "arxiv.org",
    # --- Đại học quốc tế & khóa học mở uy tín ---
    "mit.edu",
    "harvard.edu",
    "stanford.edu",
    "cmu.edu",
    "berkeley.edu",
    "openstax.org",
    "khanacademy.org",
    # --- Tài liệu kỹ thuật chính thức & công nghệ lõi ---
    "developer.mozilla.org",
    "docs.python.org",
    "docs.oracle.com",
    "learn.microsoft.com",
    "react.dev",
    "angular.dev",
    "vuejs.org",
    "nodejs.org",
    "typescriptlang.org",
    "spring.io",
    "docs.djangoproject.com",
    "fastapi.tiangolo.com",
    "flask.palletsprojects.com",
    "postgresql.org",
    "mongodb.com",
    "numpy.org",
    "pandas.pydata.org",
    "scikit-learn.org",
    "pytorch.org",
    "tensorflow.org",
    "huggingface.co",
    "git-scm.com",
    "docs.docker.com",
    "kubernetes.io",
    "docs.aws.amazon.com",
    "cloud.google.com",
    "developer.android.com",
    "developer.apple.com",
    "w3.org",
    "ietf.org",
    # --- Việt Nam: cơ quan quản lý & trường đại học chính thức ---
    "moet.gov.vn",
    "vnu.edu.vn",
    "hust.edu.vn",
    "hcmut.edu.vn",
    "uit.edu.vn",
    "hcmus.edu.vn",
    "fpt.edu.vn",
    "ptit.edu.vn",
    "ctu.edu.vn",
    "neu.edu.vn",
    "ueh.edu.vn",
    "tdtu.edu.vn",
    "udn.vn",
]

# ---------------------------------------------------------------------------
# Tier 2: Tham khảo có biên tập (dùng để giải thích, ví dụ minh họa)
# ---------------------------------------------------------------------------
TIER2_REFERENCE_DOMAINS: list[str] = [
    "wikipedia.org",
    "britannica.com",
    "refactoring.guru",
    "martinfowler.com",
    "baeldung.com",
    "freecodecamp.org",
    "digitalocean.com",
    "redhat.com",
    "geeksforgeeks.org",
    "kaggle.com",
    "mathworld.wolfram.com",
    "coursera.org",
    "edx.org",
    "vnexpress.net",
    "tuoitre.vn",
    "thanhnien.vn",
]

# ---------------------------------------------------------------------------
# Tier 3: Cộng đồng (chỉ dùng bổ trợ, luôn đối chiếu với Tier 1/2)
# ---------------------------------------------------------------------------
TIER3_COMMUNITY_DOMAINS: list[str] = [
    "stackoverflow.com",
    "github.com",
    "viblo.asia",
]

DEFAULT_TRUSTED_WEB_DOMAINS: list[str] = (
    TIER1_OFFICIAL_DOMAINS + TIER2_REFERENCE_DOMAINS + TIER3_COMMUNITY_DOMAINS
)

# ---------------------------------------------------------------------------
# Chặn hoàn toàn (Mạng xã hội, web rác, gian lận học thuật & giải đề lậu)
# ---------------------------------------------------------------------------
DEFAULT_BLOCKED_WEB_DOMAINS: list[str] = [
    # Mạng xã hội & diễn đàn hỏi đáp phi học thuật
    "pinterest.com",
    "quora.com",
    "tiktok.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "reddit.com",
    # Nội dung do người dùng tự do đăng, chất lượng không kiểm duyệt
    "medium.com",
    "dev.to",
    "w3schools.com",
    # Site bài giải / bán tài liệu lậu quốc tế
    "chegg.com",
    "coursehero.com",
    "studocu.com",
    "scribd.com",
    "slideshare.net",
    # Site giải bài tập / văn mẫu / tài liệu lậu tại Việt Nam
    "vietjack.com",
    "loigiaihay.com",
    "hoc247.net",
    "tailieu.vn",
    "123docz.net",
]

# Search depth: "basic" (fast, lightweight) or "advanced" (deeper content inspection)
TAVILY_SEARCH_DEPTH: str = os.getenv("TAVILY_SEARCH_DEPTH", "basic")

# Number of top web results to fetch
TAVILY_MAX_RESULTS: int = int(os.getenv("TAVILY_MAX_RESULTS", "3"))

# Fallback: if trusted domains return 0 results, search the broader web (still blocking spam)
FALLBACK_TO_GENERAL_SEARCH: bool = True

TRUSTED_WEB_DOMAINS: list[str] = list(DEFAULT_TRUSTED_WEB_DOMAINS)
BLOCKED_WEB_DOMAINS: list[str] = list(DEFAULT_BLOCKED_WEB_DOMAINS)


def _host(url: str) -> str:
    """Trích xuất hostname an toàn từ URL, kể cả khi thiếu scheme."""
    if "://" not in url:
        url = "https://" + url
    return (urlparse(url).hostname or "").lower()


def _matches(host: str, domains: list[str]) -> bool:
    """Khớp chính xác hoặc subdomain an toàn theo ranh giới dấu chấm."""
    return any(host == d or host.endswith("." + d) for d in domains)


def get_source_tier(url: str) -> int | None:
    """Trả về 1/2/3 theo mức độ uy tín học thuật, None nếu bị chặn hoặc không thuộc danh sách."""
    host = _host(url)
    if not host or _matches(host, DEFAULT_BLOCKED_WEB_DOMAINS):
        return None

    # Tự động xếp các cơ quan quản lý và trường đại học Việt Nam (.gov.vn, .edu.vn) vào Tier 1
    if host.endswith((".gov.vn", ".edu.vn")):
        return 1

    if _matches(host, TIER1_OFFICIAL_DOMAINS):
        return 1
    if _matches(host, TIER2_REFERENCE_DOMAINS):
        return 2
    if _matches(host, TIER3_COMMUNITY_DOMAINS):
        return 3
    return None
