import httpx
import pytest

from plugins.search.client import (
    EXA_SEARCH_URL,
    DdgsSearchClient,
    ExaSearchClient,
    FallbackSearchClient,
    SearchUnavailableError,
    SearxngSearchClient,
    create_search_client,
)


@pytest.mark.asyncio
async def test_exa_search_maps_results() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "Example",
                        "url": "https://example.com",
                        "highlights": ["Relevant excerpt"],
                        "highlightScores": [0.7, 0.8],
                    }
                ]
            },
        )

    client = ExaSearchClient(
        api_key="test-key",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    results = await client.search(query="latest ai news", max_results=2)

    assert len(results) == 1
    assert results[0].title == "Example"
    assert results[0].url == "https://example.com"
    assert results[0].content == "Relevant excerpt"
    assert results[0].score == 0.8

    assert requests[0].url == httpx.URL(EXA_SEARCH_URL)
    assert requests[0].headers["x-api-key"] == "test-key"
    assert requests[0].read() == (
        b'{"query":"latest ai news","type":"auto","numResults":2,'
        b'"contents":{"highlights":true}}'
    )


@pytest.mark.asyncio
async def test_exa_search_supports_deep_search() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    client = ExaSearchClient(
        api_key="test-key",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    await client.search(query="latest ai news", max_results=10, search_type="deep")

    assert b'"type":"deep"' in requests[0].read()


@pytest.mark.asyncio
async def test_exa_search_clamps_result_count_to_jarvis_limit() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    client = ExaSearchClient(
        api_key="test-key",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    await client.search(query="latest ai news", max_results=0)
    await client.search(query="latest ai news", max_results=101)

    assert b'"numResults":1' in requests[0].read()
    assert b'"numResults":10' in requests[1].read()


@pytest.mark.asyncio
async def test_searxng_search_maps_results() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "results": [
                    {
                        "title": "SearX Result",
                        "url": "https://example.org",
                        "content": "Snippet text",
                        "score": 0.9,
                    }
                ]
            },
        )

    client = SearxngSearchClient(
        base_url="http://127.0.0.1:8080",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    results = await client.search(query="python async", max_results=3)

    assert len(results) == 1
    assert results[0].title == "SearX Result"
    assert results[0].url == "https://example.org"
    assert results[0].content == "Snippet text"
    assert results[0].score == 0.9

    assert "format=json" in str(requests[0].url)
    assert "categories=general" in str(requests[0].url)


@pytest.mark.asyncio
async def test_searxng_rejects_403_json_disabled() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    client = SearxngSearchClient(
        base_url="http://127.0.0.1:8080",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    with pytest.raises(ValueError, match="JSON output is not enabled"):
        await client.search(query="test", max_results=3)


@pytest.mark.asyncio
async def test_searxng_rejects_html_response() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>")

    client = SearxngSearchClient(
        base_url="http://127.0.0.1:8080",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    with pytest.raises(ValueError, match="non-JSON response"):
        await client.search(query="test", max_results=3)


@pytest.mark.asyncio
async def test_fallback_prefers_searxng_then_exa() -> None:
    call_order: list[str] = []

    async def searxng_handler(_request: httpx.Request) -> httpx.Response:
        call_order.append("searxng")
        raise httpx.ConnectError("connection refused")

    async def exa_handler(_request: httpx.Request) -> httpx.Response:
        call_order.append("exa")
        return httpx.Response(
            200,
            json={"results": [{"title": "Exa", "url": "https://exa.ai", "text": "body"}]},
        )

    searxng = SearxngSearchClient(
        base_url="http://127.0.0.1:8080",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(searxng_handler)
        ),
    )
    exa = ExaSearchClient(
        api_key="test-key",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(exa_handler)
        ),
    )
    client = FallbackSearchClient([searxng, exa])

    results = await client.search(query="test", max_results=3)

    assert call_order == ["searxng", "exa"]
    assert results[0].title == "Exa"


@pytest.mark.asyncio
async def test_fallback_uses_exa_first_for_deep_search() -> None:
    call_order: list[str] = []

    async def searxng_handler(_request: httpx.Request) -> httpx.Response:
        call_order.append("searxng")
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"results": [{"title": "SearXNG", "url": "https://searxng.org"}]},
        )

    async def exa_handler(_request: httpx.Request) -> httpx.Response:
        call_order.append("exa")
        return httpx.Response(
            200,
            json={"results": [{"title": "Exa", "url": "https://exa.ai", "text": "deep"}]},
        )

    searxng = SearxngSearchClient(
        base_url="http://127.0.0.1:8080",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(searxng_handler)
        ),
    )
    exa = ExaSearchClient(
        api_key="test-key",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(exa_handler)
        ),
    )
    client = FallbackSearchClient([searxng, exa])

    results = await client.search(query="test", max_results=3, search_type="deep")

    assert call_order == ["exa"]
    assert results[0].title == "Exa"


@pytest.mark.asyncio
async def test_fallback_raises_when_all_providers_fail() -> None:
    async def fail_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    searxng = SearxngSearchClient(
        base_url="http://127.0.0.1:8080",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(fail_handler)
        ),
    )

    client = FallbackSearchClient([searxng])

    with pytest.raises(SearchUnavailableError, match="All search providers failed"):
        await client.search(query="test", max_results=3)


@pytest.mark.asyncio
async def test_ddgs_search_maps_results() -> None:
    calls: list[tuple[str, int]] = []

    def fake_text(query: str, max_results: int) -> list[dict]:
        calls.append((query, max_results))
        return [
            {
                "title": "DDGS Result",
                "href": "https://example.com/ddgs",
                "body": "Snippet from built-in search",
            }
        ]

    client = DdgsSearchClient(text_search=fake_text)
    results = await client.search(query="python async", max_results=3)

    assert calls == [("python async", 3)]
    assert len(results) == 1
    assert results[0].title == "DDGS Result"
    assert results[0].url == "https://example.com/ddgs"
    assert results[0].content == "Snippet from built-in search"


@pytest.mark.asyncio
async def test_ddgs_search_clamps_result_count() -> None:
    seen: list[int] = []

    def fake_text(_query: str, max_results: int) -> list[dict]:
        seen.append(max_results)
        return [{"title": "x", "href": "https://example.com", "body": "y"}]

    client = DdgsSearchClient(text_search=fake_text)
    await client.search(query="q", max_results=0)
    await client.search(query="q", max_results=101)

    assert seen == [1, 10]


@pytest.mark.asyncio
async def test_ddgs_search_empty_results_are_failure() -> None:
    client = DdgsSearchClient(text_search=lambda _q, _n: [])
    with pytest.raises(SearchUnavailableError, match="no results"):
        await client.search(query="q", max_results=3)


@pytest.mark.asyncio
async def test_ddgs_search_normalizes_exceptions() -> None:
    from ddgs.exceptions import DDGSException

    def boom(_query: str, _max_results: int) -> list[dict]:
        raise DDGSException("upstream blocked")

    client = DdgsSearchClient(text_search=boom)
    with pytest.raises(SearchUnavailableError, match="Built-in search failed"):
        await client.search(query="q", max_results=3)


def test_create_search_client_includes_ddgs_when_no_optional_providers(monkeypatch) -> None:
    monkeypatch.setattr("plugins.search.client.settings.SEARXNG_URL", None)
    monkeypatch.setattr(
        "plugins.search.client.credential_store.get_stored_secret",
        lambda _name: None,
    )

    client = create_search_client({})
    assert len(client._providers) == 1
    assert isinstance(client._providers[0], DdgsSearchClient)


def test_create_search_client_order_searxng_exa_ddgs(monkeypatch) -> None:
    monkeypatch.setattr(
        "plugins.search.client.settings.SEARXNG_URL",
        "http://127.0.0.1:8080",
    )
    monkeypatch.setattr(
        "plugins.search.client.credential_store.get_stored_secret",
        lambda name: "exa-key" if name == "EXA_API_KEY" else None,
    )

    client = create_search_client({})
    types = [type(provider) for provider in client._providers]
    assert types == [SearxngSearchClient, ExaSearchClient, DdgsSearchClient]


def test_deep_search_still_promotes_exa(monkeypatch) -> None:
    monkeypatch.setattr(
        "plugins.search.client.settings.SEARXNG_URL",
        "http://127.0.0.1:8080",
    )
    monkeypatch.setattr(
        "plugins.search.client.credential_store.get_stored_secret",
        lambda name: "exa-key" if name == "EXA_API_KEY" else None,
    )

    client = create_search_client({})
    ordered = client._providers_for("deep")
    assert isinstance(ordered[0], ExaSearchClient)
    assert [type(provider) for provider in ordered] == [
        ExaSearchClient,
        SearxngSearchClient,
        DdgsSearchClient,
    ]