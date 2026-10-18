"""Pluggable search backends for WebSearchTool: DuckDuckGo, Tavily, and Serper.

DuckDuckGo is the default zero-config backend requiring no API key. Tavily and Serper
are optional premium backends activated when their API keys are configured.
"""

from __future__ import annotations

import html
import logging
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.core.config import Settings
from app.tools.errors import ToolNetworkError, ToolTimeoutError
from app.tools.web_search import SearchBackend, SearchResult

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class _DDGHTMLParser(HTMLParser):
    """Parses html.duckduckgo.com/html/ result pages into titles, URLs, and snippets."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._in_title = False
        self._in_snippet = False
        self._current_url = ""
        self._current_title: list[str] = []
        self._current_snippet: list[str] = []

    def _commit_current(self) -> None:
        title = html.unescape("".join(self._current_title)).strip()
        snippet = html.unescape("".join(self._current_snippet)).strip()
        url = self._current_url.strip()
        if url and (url.startswith("http://") or url.startswith("https://")) and (title or snippet):
            self.results.append({"title": title or url, "url": url, "snippet": snippet or title})

    def _reset_current(self) -> None:
        self._current_url = ""
        self._current_title = []
        self._current_snippet = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = dict(attrs)
        classes = attr_dict.get("class", "").split()

        if tag == "div" and any(c in classes for c in ("result", "web-result")):
            self._commit_current()
            self._reset_current()
        elif tag == "a" and "result__a" in classes:
            self._in_title = True
            self._current_title = []
            raw_href = attr_dict.get("href", "")
            resolved = self._extract_target_url(raw_href)
            if resolved:
                self._current_url = resolved
        elif tag == "a" and "result__snippet" in classes:
            self._in_snippet = True
            self._current_snippet = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        elif tag == "a" and self._in_snippet:
            self._in_snippet = False

    def close(self) -> None:
        super().close()
        self._commit_current()
        self._reset_current()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._current_title.append(data)
        elif self._in_snippet:
            self._current_snippet.append(data)

    @staticmethod
    def _extract_target_url(raw_href: str) -> str | None:
        if not raw_href:
            return None
        if "uddg=" in raw_href:
            parsed = urlparse(raw_href)
            params = parse_qs(parsed.query)
            if "uddg" in params and params["uddg"]:
                return unquote(params["uddg"][0])
        if raw_href.startswith("//"):
            raw_href = "https:" + raw_href
        if raw_href.startswith(("http://", "https://")) and "duckduckgo.com" not in raw_href:
            return raw_href
        return None


class DuckDuckGoSearchBackend:
    """Zero-configuration web search using DuckDuckGo HTML endpoint."""

    name = "duckduckgo"
    fake = False

    def __init__(self, timeout_seconds: float = 12.0) -> None:
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
        }
        url = "https://html.duckduckgo.com/html/"
        data = {"q": query, "b": ""}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                resp = await client.post(url, headers=headers, data=data)
        except httpx.TimeoutException as exc:
            raise ToolTimeoutError(f"DuckDuckGo search timed out after {self.timeout_seconds}s") from exc
        except httpx.RequestError as exc:
            raise ToolNetworkError(f"DuckDuckGo search request failed: {exc}") from exc

        if resp.status_code != 200:
            # Fall back to GET if POST is throttled
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds, follow_redirects=True) as client:
                    resp = await client.get(url, headers=headers, params={"q": query})
            except Exception as exc:
                raise ToolNetworkError(f"DuckDuckGo search HTTP error {resp.status_code}") from exc

        parser = _DDGHTMLParser()
        parser.feed(resp.text)
        parser.close()

        results: list[SearchResult] = []
        seen_urls: set[str] = set()

        for item in parser.results:
            target_url = item["url"]
            if target_url in seen_urls:
                continue
            seen_urls.add(target_url)
            results.append(
                SearchResult(
                    title=item["title"][:500],
                    url=target_url[:2000],
                    snippet=item["snippet"][:2000],
                    source=self.name,
                )
            )
            if len(results) >= max_results:
                break

        return results


class TavilySearchBackend:
    """Tavily search API backend (fast, LLM-optimized snippets)."""

    name = "tavily"
    fake = False

    def __init__(self, api_key: str, timeout_seconds: float = 15.0) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        url = "https://api.tavily.com/search"
        payload = {
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(url, json=payload)
        except httpx.TimeoutException as exc:
            raise ToolTimeoutError(f"Tavily search timed out after {self.timeout_seconds}s") from exc
        except httpx.RequestError as exc:
            raise ToolNetworkError(f"Tavily search connection error: {exc}") from exc

        if resp.status_code != 200:
            raise ToolNetworkError(f"Tavily search returned HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        raw_results = data.get("results", [])
        results: list[SearchResult] = []

        for item in raw_results[:max_results]:
            u = item.get("url", "")
            if not u.startswith(("http://", "https://")):
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title", ""))[:500] or u,
                    url=u[:2000],
                    snippet=str(item.get("content", ""))[:2000],
                    source=self.name,
                )
            )
        return results


class SerperSearchBackend:
    """Google Serper search backend."""

    name = "serper"
    fake = False

    def __init__(self, api_key: str, timeout_seconds: float = 15.0) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def search(self, query: str, max_results: int = 5) -> list[SearchResult]:
        url = "https://google.serper.dev/search"
        headers = {
            "X-API-KEY": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {"q": query, "num": max_results}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.TimeoutException as exc:
            raise ToolTimeoutError(f"Serper search timed out after {self.timeout_seconds}s") from exc
        except httpx.RequestError as exc:
            raise ToolNetworkError(f"Serper search connection error: {exc}") from exc

        if resp.status_code != 200:
            raise ToolNetworkError(f"Serper search returned HTTP {resp.status_code}: {resp.text[:200]}")

        data = resp.json()
        organic = data.get("organic", [])
        results: list[SearchResult] = []

        for item in organic[:max_results]:
            u = item.get("link", "")
            if not u.startswith(("http://", "https://")):
                continue
            results.append(
                SearchResult(
                    title=str(item.get("title", ""))[:500] or u,
                    url=u[:2000],
                    snippet=str(item.get("snippet", ""))[:2000],
                    source=self.name,
                )
            )
        return results


def build_search_backend(settings: Settings) -> SearchBackend:
    """Factory creating the appropriate search backend based on settings and available keys."""
    backend_choice = getattr(settings, "web_search_backend", "duckduckgo")

    if backend_choice == "tavily":
        key = getattr(settings, "tavily_api_key", None)
        if key and key.get_secret_value():
            return TavilySearchBackend(key.get_secret_value())
        logger.warning("Tavily search backend configured, but TAVILY_API_KEY is not set. Falling back to DuckDuckGo.")

    elif backend_choice == "serper":
        key = getattr(settings, "serper_api_key", None)
        if key and key.get_secret_value():
            return SerperSearchBackend(key.get_secret_value())
        logger.warning("Serper search backend configured, but SERPER_API_KEY is not set. Falling back to DuckDuckGo.")

    return DuckDuckGoSearchBackend()
