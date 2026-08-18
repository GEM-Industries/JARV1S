"""Search provider clients — SearXNG, Exa, and built-in DDGS."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urljoin

import httpx
from ddgs import DDGS
from ddgs.exceptions import DDGSException
from pydantic import BaseModel

from core import settings
from core.credentials.store import credential_store

logger = logging.getLogger(__name__)

EXA_SEARCH_URL = "https://api.exa.ai/search"
MAX_CONTENT_LENGTH = 500
DEFAULT_MAX_RESULTS = 3
MIN_RESULTS = 1
MAX_RESULTS = 10
SearchType = Literal["auto", "deep"]
SEARCH_TIMEOUT_S = 20.0
DDGS_TIMEOUT_S = int(SEARCH_TIMEOUT_S)
SEARXNG_TIMEOUT = httpx.Timeout(5.0, connect=2.0)


class SearchResult(BaseModel):
    """Structured result from a web search."""
    title: str
    url: str
    content: str
    score: float = 0.0


class SearchUnavailableError(Exception):
    """Raised when all search providers fail."""


class SearchClient(Protocol):
    async def search(
        self,
        query: str,
        max_results: int,
        search_type: SearchType = "auto",
    ) -> list[SearchResult]: ...


@dataclass(frozen=True, slots=True)
class SearchProviderStatus:
    searxng_configured: bool
    exa_configured: bool


def get_search_provider_status() -> SearchProviderStatus:
    """Pure helper for setup lanes — no live HTTP probes."""
    searxng_url = (settings.SEARXNG_URL or "").strip()
    exa_key = credential_store.get_stored_secret("EXA_API_KEY")
    return SearchProviderStatus(
        searxng_configured=bool(searxng_url),
        exa_configured=bool(exa_key),
    )


def _clamp_results(max_results: int) -> int:
    return max(MIN_RESULTS, min(max_results, MAX_RESULTS))


def _normalize_search_type(search_type: str) -> SearchType:
    if search_type == "deep":
        return "deep"
    return "auto"


def _truncate_content(content: str) -> str:
    if len(content) > MAX_CONTENT_LENGTH:
        return content[:MAX_CONTENT_LENGTH] + "..."
    return content


class SearxngSearchClient:
    """Async client for a self-hosted SearXNG JSON API."""

    def __init__(
        self,
        base_url: str,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=SEARXNG_TIMEOUT)
        )

    async def search(
        self,
        query: str,
        max_results: int,
        search_type: SearchType = "auto",
    ) -> list[SearchResult]:
        _ = search_type  # SearXNG has no deep-search mode; Exa handles that on fallback.
        max_results = _clamp_results(max_results)
        url = urljoin(self._base_url + "/", "search")
        async with self._http_client_factory() as http:
            response = await http.get(
                url,
                params={
                    "q": query,
                    "format": "json",
                    "categories": "general",
                    "pageno": 1,
                },
            )
        if response.status_code == 403:
            raise ValueError(
                "SearXNG JSON output is not enabled — add 'json' to search.formats in settings.yml"
            )
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type.lower():
            raise ValueError(
                "SearXNG returned non-JSON response — JSON format may not be enabled on this instance"
            )

        try:
            data = response.json()
        except Exception as exc:
            raise ValueError("SearXNG returned invalid JSON") from exc

        results: list[SearchResult] = []
        for item in (data.get("results") or [])[:max_results]:
            snippet = str(item.get("content") or item.get("snippet") or "")
            results.append(SearchResult(
                title=item.get("title") or "Unknown",
                url=item.get("url") or "",
                content=_truncate_content(snippet),
                score=float(item.get("score") or 0.0),
            ))
        return results


class ExaSearchClient:
    """Small async client for Exa search."""

    def __init__(
        self,
        api_key: str,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._api_key = api_key
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=SEARCH_TIMEOUT_S)
        )

    async def search(
        self,
        query: str,
        max_results: int,
        search_type: SearchType = "auto",
    ) -> list[SearchResult]:
        max_results = _clamp_results(max_results)
        search_type = _normalize_search_type(search_type)
        async with self._http_client_factory() as http:
            response = await http.post(
                EXA_SEARCH_URL,
                headers={
                    "x-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                json={
                    "query": query,
                    "type": search_type,
                    "numResults": max_results,
                    "contents": {"highlights": True},
                },
            )
        response.raise_for_status()

        results: list[SearchResult] = []
        for item in response.json().get("results", []):
            results.append(SearchResult(
                title=item.get("title") or "Unknown",
                url=item.get("url") or "",
                content=_exa_result_content(item),
                score=_exa_result_score(item),
            ))
        return results


def _exa_result_content(item: dict) -> str:
    highlights = item.get("highlights") or []
    if highlights:
        content = "\n".join(str(highlight) for highlight in highlights if highlight)
    else:
        content = str(item.get("text") or item.get("summary") or "")
    return _truncate_content(content)


def _exa_result_score(item: dict) -> float:
    highlight_scores = item.get("highlightScores") or []
    if highlight_scores:
        return float(max(highlight_scores))
    return float(item.get("score") or 0.0)


def _ddgs_text_search(query: str, max_results: int) -> list[dict[str, Any]]:
    """Blocking DDGS call — always run via asyncio.to_thread."""
    with DDGS(timeout=DDGS_TIMEOUT_S) as client:
        return client.text(
            query,
            backend="auto",
            safesearch="moderate",
            max_results=max_results,
        )


class DdgsSearchClient:
    """In-process keyless search via the ddgs package."""

    def __init__(
        self,
        text_search: Callable[[str, int], list[dict[str, Any]]] | None = None,
    ) -> None:
        self._text_search = text_search or _ddgs_text_search

    async def search(
        self,
        query: str,
        max_results: int,
        search_type: SearchType = "auto",
    ) -> list[SearchResult]:
        _ = search_type
        max_results = _clamp_results(max_results)
        try:
            raw = await asyncio.to_thread(self._text_search, query, max_results)
        except DDGSException as exc:
            raise SearchUnavailableError(f"Built-in search failed: {exc}") from exc

        if not raw:
            raise SearchUnavailableError("Built-in search returned no results")

        results: list[SearchResult] = []
        for item in raw[:max_results]:
            results.append(SearchResult(
                title=str(item.get("title") or "Unknown"),
                url=str(item.get("href") or item.get("url") or ""),
                content=_truncate_content(str(item.get("body") or item.get("content") or "")),
            ))
        return results


class FallbackSearchClient:
    """Try providers in order; fall back on failure."""

    def __init__(self, providers: list[SearchClient]) -> None:
        if not providers:
            raise SearchUnavailableError("No search providers available")
        self._providers = providers

    async def search(
        self,
        query: str,
        max_results: int,
        search_type: SearchType = "auto",
    ) -> list[SearchResult]:
        last_error: Exception | None = None
        for provider in self._providers_for(search_type):
            try:
                return await provider.search(query, max_results, search_type)
            except (httpx.HTTPError, ValueError, SearchUnavailableError) as exc:
                logger.warning("Search provider failed, trying next: %s", exc)
                last_error = exc
        raise SearchUnavailableError(
            f"All search providers failed: {last_error}"
        ) from last_error

    def _providers_for(self, search_type: SearchType) -> list[SearchClient]:
        if search_type != "deep":
            return self._providers
        exa = [provider for provider in self._providers if isinstance(provider, ExaSearchClient)]
        if not exa:
            return self._providers
        return exa + [provider for provider in self._providers if not isinstance(provider, ExaSearchClient)]


def create_search_client(config: dict) -> FallbackSearchClient:
    """Factory: SearXNG → Exa → built-in DDGS."""
    _ = config
    providers: list[SearchClient] = []

    searxng_url = (settings.SEARXNG_URL or "").strip()
    if searxng_url:
        providers.append(SearxngSearchClient(searxng_url))

    exa_key = credential_store.get_stored_secret("EXA_API_KEY")
    if exa_key:
        providers.append(ExaSearchClient(exa_key))

    providers.append(DdgsSearchClient())
    return FallbackSearchClient(providers)
