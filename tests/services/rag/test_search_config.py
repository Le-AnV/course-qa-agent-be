"""Tests for source reputation tiers and SerpApi result ranking."""

from __future__ import annotations

from app.config import search_config, settings
from app.services import rag_service


def test_trusted_and_blocked_web_domains_configured() -> None:
    # Tier 1
    assert "developer.mozilla.org" in search_config.TIER1_OFFICIAL_DOMAINS
    assert "mit.edu" in search_config.TIER1_OFFICIAL_DOMAINS
    assert "hust.edu.vn" in search_config.TIER1_OFFICIAL_DOMAINS

    # Tier 2
    assert "wikipedia.org" in search_config.TIER2_REFERENCE_DOMAINS
    assert "geeksforgeeks.org" in search_config.TIER2_REFERENCE_DOMAINS
    assert "refactoring.guru" in search_config.TIER2_REFERENCE_DOMAINS

    # Tier 3
    assert "viblo.asia" in search_config.TIER3_COMMUNITY_DOMAINS

    # Blocked
    assert "w3schools.com" in search_config.BLOCKED_WEB_DOMAINS
    assert "chegg.com" in search_config.BLOCKED_WEB_DOMAINS
    assert "vietjack.com" in search_config.BLOCKED_WEB_DOMAINS
    assert "quora.com" in search_config.BLOCKED_WEB_DOMAINS


def test_get_source_tier() -> None:
    # Tier 1: Official specs & universities
    assert search_config.get_source_tier("https://developer.mozilla.org/en-US/docs/Web") == 1
    assert search_config.get_source_tier("https://cs.stanford.edu/courses") == 1
    assert search_config.get_source_tier("https://daotao.hcmut.edu.vn/syllabus") == 1
    assert search_config.get_source_tier("https://dut.udn.vn/khoa-cntt") == 1  # auto .edu.vn
    assert search_config.get_source_tier("https://moet.gov.vn/van-ban") == 1  # auto .gov.vn

    # Tier 2: Reference
    assert search_config.get_source_tier("https://en.wikipedia.org/wiki/OOP") == 2
    assert search_config.get_source_tier("https://refactoring.guru/design-patterns") == 2

    # Tier 3: Community
    assert search_config.get_source_tier("https://stackoverflow.com/questions/123") == 3
    assert search_config.get_source_tier("https://viblo.asia/p/abc") == 3

    # Blocked: returns None
    assert search_config.get_source_tier("https://vietjack.com/bai-tap-java") is None
    assert search_config.get_source_tier("https://chegg.com/homework-help") is None
    assert search_config.get_source_tier("https://w3schools.com/python") is None
    assert search_config.get_source_tier("https://random-unverified-blog.xyz") is None


def test_is_blocked_only_matches_the_hard_blocklist() -> None:
    assert search_config.is_blocked("https://www.w3schools.com/python")
    assert search_config.is_blocked("https://old.reddit.com/r/learnpython")
    assert not search_config.is_blocked("https://docs.python.org/3/library/smtplib.html")
    assert not search_config.is_blocked("https://random-unverified-blog.xyz/post")


def test_search_internet_drops_blocked_sites_and_sorts_by_tier(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "organic_results": [
                    {
                        "title": "Viblo Community Post",
                        "link": "https://viblo.asia/p/java-tips",
                        "snippet": "Community tips on Java...",
                    },
                    {
                        "title": "W3Schools Tutorial",
                        "link": "https://www.w3schools.com/python/",
                        "snippet": "Blocked tutorial site...",
                    },
                    {
                        "title": "MDN Official Documentation",
                        "link": "https://developer.mozilla.org/en-US/docs/Web/JavaScript",
                        "snippet": "Official Web Documentation...",
                    },
                    {
                        "title": "smtplib documentation",
                        "link": "https://docs.python.org/3/library/smtplib.html",
                        "snippet": "Official Python documentation...",
                    },
                    {
                        "title": "Small personal blog",
                        "link": "https://random-unverified-blog.xyz/post",
                        "snippet": "Unranked domain...",
                    },
                ]
            }

    class FakeClient:
        def get(self, url, params=None):
            return FakeResponse()

    monkeypatch.setattr(settings, "serpapi_api_key", "test-key")
    monkeypatch.setattr(rag_service, "_get_search_client", lambda: FakeClient())

    results = rag_service.search_internet("JavaScript closures")

    urls = [item.url for item in results]
    assert all("w3schools.com" not in url for url in urls)
    assert len(results) == 4
    assert urls[0].startswith("https://developer.mozilla.org")
    assert urls[1].startswith("https://docs.python.org")
    assert urls[2].startswith("https://viblo.asia")
    assert urls[3].startswith("https://random-unverified-blog.xyz")


def test_search_internet_keeps_at_most_five_results(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "organic_results": [
                    {
                        "title": f"Result {index}",
                        "link": f"https://example{index}.com/page",
                        "snippet": "snippet",
                    }
                    for index in range(8)
                ]
            }

    class FakeClient:
        def get(self, url, params=None):
            return FakeResponse()

    monkeypatch.setattr(settings, "serpapi_api_key", "test-key")
    monkeypatch.setattr(rag_service, "_get_search_client", lambda: FakeClient())

    assert len(rag_service.search_internet("python email")) == 5
