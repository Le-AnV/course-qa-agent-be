"""Tests for search_config and Tavily payload integration."""

from __future__ import annotations

from unittest.mock import MagicMock

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
    assert search_config.get_source_tier("https://moet.gov.vn/van-ban") == 1   # auto .gov.vn

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


def test_search_internet_uses_trusted_domains_and_sorts_by_tier(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tavily_api_key", "test-tavily-key")

    captured_requests: list[dict] = []

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.is_success = True
    mock_response.raise_for_status = MagicMock()
    # Return results in mixed order: Tier 3 (Viblo) first, Tier 1 (Mozilla) second
    mock_response.json.return_value = {
        "results": [
            {
                "title": "Viblo Community Post",
                "url": "https://viblo.asia/p/java-tips",
                "content": "Community tips on Java...",
            },
            {
                "title": "MDN Official Documentation",
                "url": "https://developer.mozilla.org/en-US/docs/Web/JavaScript",
                "content": "Official Web Documentation...",
            },
        ]
    }

    class MockClient:
        def post(self, url, json=None):
            captured_requests.append({"url": url, "json": json})
            return mock_response

    monkeypatch.setattr(rag_service, "_get_tavily_client", lambda: MockClient())

    results = rag_service.search_internet("JavaScript closures")

    assert len(results) == 2
    # Tier 1 (MDN) is sorted before Tier 3 (Viblo)
    assert results[0].title == "MDN Official Documentation"
    assert "developer.mozilla.org" in results[0].url
    assert results[1].title == "Viblo Community Post"
    assert "viblo.asia" in results[1].url

    assert len(captured_requests) == 1
    req = captured_requests[0]["json"]
    assert req["include_domains"] == search_config.TRUSTED_WEB_DOMAINS
    assert req["exclude_domains"] == search_config.BLOCKED_WEB_DOMAINS


def test_search_internet_falls_back_when_trusted_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(settings, "tavily_api_key", "test-tavily-key")

    call_count = 0

    def mock_post(url, json=None):
        nonlocal call_count
        call_count += 1
        resp = MagicMock()
        resp.status_code = 200
        resp.is_success = True
        resp.raise_for_status = MagicMock()
        if call_count == 1:
            resp.json.return_value = {"results": []}
        else:
            resp.json.return_value = {
                "results": [
                    {
                        "title": "General Web Result",
                        "url": "https://example-academic.org/niche-topic",
                        "content": "Niche topic explanation...",
                    }
                ]
            }
        return resp

    class MockClient:
        def post(self, url, json=None):
            return mock_post(url, json)

    monkeypatch.setattr(rag_service, "_get_tavily_client", lambda: MockClient())

    results = rag_service.search_internet("Some very niche university concept")

    assert len(results) == 1
    assert results[0].title == "General Web Result"
    assert call_count == 2
