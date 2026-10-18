"""Tests for pluggable web search backends (DuckDuckGo, Tavily, Serper) and WebSearchTool."""

from typing import Any
import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.tools.factory import build_tool_registry
from app.tools.search_backends import (
    DuckDuckGoSearchBackend,
    SerperSearchBackend,
    TavilySearchBackend,
    _DDGHTMLParser,
    build_search_backend,
)
from app.tools.schemas import ToolContext
from app.tools.web_search import SearchQuery, WebSearchTool


SAMPLE_DDG_HTML = """
<!DOCTYPE html>
<html>
<body>
<div class="results">
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fanime&rut=...">
          Latest <b>Anime</b> Streaming - Fall 2026
        </a>
      </h2>
      <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fanime">
        Discover the latest anime releases streaming this month on Crunchyroll, Netflix, and Hulu.
      </a>
      <div class="result__extras">
        <a class="result__url" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fanime">
          example.com/anime
        </a>
      </div>
    </div>
  </div>
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a class="result__a" href="https://myanimelist.net/season/fall">
          MyAnimeList Fall 2026 Season
        </a>
      </h2>
      <a class="result__snippet" href="https://myanimelist.net/season/fall">
        Full list of anime broadcast schedules and streaming dates.
      </a>
    </div>
  </div>
</div>
</body>
</html>
"""


def test_ddg_html_parser_extracts_results_and_unquotes_urls() -> None:
    parser = _DDGHTMLParser()
    parser.feed(SAMPLE_DDG_HTML)
    parser.close()

    assert len(parser.results) == 2
    first = parser.results[0]
    assert first["url"] == "https://example.com/anime"
    assert "Latest Anime Streaming" in first["title"]
    assert "Crunchyroll" in first["snippet"]

    second = parser.results[1]
    assert second["url"] == "https://myanimelist.net/season/fall"
    assert "MyAnimeList Fall 2026" in second["title"]


async def test_duckduckgo_backend_with_mocked_http(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = DuckDuckGoSearchBackend()

    async def mock_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_DDG_HTML, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    results = await backend.search("anime releases", max_results=1)
    assert len(results) == 1
    assert results[0].url == "https://example.com/anime"
    assert results[0].source == "duckduckgo"


async def test_tavily_backend_with_mocked_http(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = TavilySearchBackend(api_key="tvly-mock-key")

    mock_data = {
        "results": [
            {"title": "Anime 2026", "url": "https://anime.example.com", "content": "Streaming guide"},
            {"title": "News", "url": "https://news.example.com", "content": "Top stories"},
        ]
    }

    async def mock_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        assert kwargs["json"]["api_key"] == "tvly-mock-key"
        return httpx.Response(200, json=mock_data, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    results = await backend.search("anime 2026", max_results=2)
    assert len(results) == 2
    assert results[0].title == "Anime 2026"
    assert results[0].source == "tavily"


async def test_serper_backend_with_mocked_http(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = SerperSearchBackend(api_key="serper-mock-key")

    mock_data = {
        "organic": [
            {"title": "Google Result 1", "link": "https://serper.example.com/1", "snippet": "Snippet 1"},
            {"title": "Google Result 2", "link": "https://serper.example.com/2", "snippet": "Snippet 2"},
        ]
    }

    async def mock_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        assert kwargs["headers"]["X-API-KEY"] == "serper-mock-key"
        return httpx.Response(200, json=mock_data, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    results = await backend.search("query", max_results=2)
    assert len(results) == 2
    assert results[0].url == "https://serper.example.com/1"
    assert results[0].source == "serper"


def test_build_search_backend_selection() -> None:
    # 1. Default -> DuckDuckGo
    s1 = Settings(_env_file=None, NEXUS_WEB_SEARCH_BACKEND="duckduckgo")  # type: ignore[call-arg]
    assert isinstance(build_search_backend(s1), DuckDuckGoSearchBackend)

    # 2. Tavily with key
    s2 = Settings(_env_file=None, NEXUS_WEB_SEARCH_BACKEND="tavily", TAVILY_API_KEY="t-key")  # type: ignore[call-arg]
    b2 = build_search_backend(s2)
    assert isinstance(b2, TavilySearchBackend) and b2.api_key == "t-key"

    # 3. Tavily without key -> fallback to DuckDuckGo
    s3 = Settings(_env_file=None, NEXUS_WEB_SEARCH_BACKEND="tavily")  # type: ignore[call-arg]
    assert isinstance(build_search_backend(s3), DuckDuckGoSearchBackend)

    # 4. Serper with key
    s4 = Settings(_env_file=None, NEXUS_WEB_SEARCH_BACKEND="serper", SERPER_API_KEY="s-key")  # type: ignore[call-arg]
    b4 = build_search_backend(s4)
    assert isinstance(b4, SerperSearchBackend) and b4.api_key == "s-key"


async def test_web_search_tool_execute_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mock_post(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, text=SAMPLE_DDG_HTML, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    from uuid import uuid4

    tool = WebSearchTool(DuckDuckGoSearchBackend())
    context = ToolContext(run_id=uuid4(), task_id="t1", agent_type="researcher", tool_call_id="c1")
    output = await tool.execute(SearchQuery(query="latest anime", max_results=2), context)

    assert output.query == "latest anime"
    assert output.backend == "duckduckgo"
    assert len(output.results) == 2
    assert output.results[0].url == "https://example.com/anime"


def test_web_search_tool_registered_in_factory() -> None:
    settings_on = Settings(_env_file=None, NEXUS_WEB_SEARCH_ENABLED=True)  # type: ignore[call-arg]
    registry_on = build_tool_registry(settings_on)
    assert "web_search" in registry_on
    assert registry_on.get("web_search").definition.name == "web_search"

    settings_off = Settings(_env_file=None, NEXUS_WEB_SEARCH_ENABLED=False)  # type: ignore[call-arg]
    registry_off = build_tool_registry(settings_off)
    assert "web_search" not in registry_off
