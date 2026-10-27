"""Internet source agent backed by SerpApi (Google Search) (AI-67)."""

from __future__ import annotations

from app.config.config import settings
from app.services import rag_service


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_search_internet_returns_nothing_without_a_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "serpapi_api_key", None)

    assert rag_service.search_internet("gửi email bằng python") == []


def test_search_internet_maps_google_organic_results(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def get(self, url, params=None):
            captured.update(url=url, params=params)
            return _FakeResponse(
                {
                    "organic_results": [
                        {
                            "title": "Send Email With Python",
                            "link": "https://realpython.com/python-send-email/",
                            "snippet": "Use smtplib to send email from Python.",
                        },
                        {"title": "Broken result without a link"},
                    ]
                }
            )

    monkeypatch.setattr(settings, "serpapi_api_key", "test-key")
    monkeypatch.setattr(rag_service, "_get_search_client", lambda: FakeClient())

    results = rag_service.search_internet("gửi email bằng python")

    assert captured["url"] == rag_service.SERPAPI_SEARCH_ENDPOINT
    params = captured["params"]
    assert params["engine"] == "google"
    assert params["q"] == "gửi email bằng python"
    assert params["api_key"] == "test-key"
    assert params["num"] == rag_service.SERPAPI_FETCH_RESULTS
    assert params["hl"] == "vi"
    assert "gl" not in params
    assert len(results) == 1
    assert results[0].title == "Send Email With Python"
    assert results[0].url.startswith("https://realpython.com/python-send-email/")
    assert results[0].content == "Use smtplib to send email from Python."


def test_search_internet_uses_english_market_for_english_questions(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def get(self, url, params=None):
            captured.update(params=params)
            return _FakeResponse({"organic_results": []})

    monkeypatch.setattr(settings, "serpapi_api_key", "test-key")
    monkeypatch.setattr(rag_service, "_get_search_client", lambda: FakeClient())

    rag_service.search_internet("python send email example")

    assert captured["params"]["hl"] == "en"
    assert "gl" not in captured["params"]


def test_search_internet_returns_nothing_when_the_provider_fails(monkeypatch) -> None:
    class BrokenClient:
        def get(self, *args, **kwargs):
            raise RuntimeError("quota exhausted")

    monkeypatch.setattr(settings, "serpapi_api_key", "test-key")
    monkeypatch.setattr(rag_service, "_get_search_client", lambda: BrokenClient())

    assert rag_service.search_internet("gửi email bằng python") == []
