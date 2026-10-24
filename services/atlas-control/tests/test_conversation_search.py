import httpx
import pytest

from app.conversation_search import SearchError, search_web


@pytest.mark.asyncio
async def test_search_returns_bounded_sources():
    def respond(request):
        assert request.url.path == "/search"
        assert request.url.params["q"] == "public test"
        assert request.url.params["format"] == "json"
        return httpx.Response(200, json={"results": [
            {"title": "Source", "url": f"https://example.org/{i}",
             "content": "x" * 2000}
            for i in range(8)
        ]})
    result = await search_web(
        "http://search:8080", "public test",
        transport=httpx.MockTransport(respond),
    )
    assert len(result["results"]) == 5
    assert len(result["results"][0]["snippet"]) == 1200
    assert result["results"][0]["source_id"] == 1
    assert "pages not fetched" in result["evidence_scope"]


@pytest.mark.asyncio
async def test_redirect_is_not_followed():
    calls = []
    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://elsewhere/"})
    with pytest.raises(SearchError, match="HTTP 302"):
        await search_web(
            "http://search:8080", "test",
            transport=httpx.MockTransport(respond),
        )
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    [], {}, {"results": "wrong"},
])
async def test_invalid_result_structure(payload):
    with pytest.raises(SearchError, match="structure"):
        await search_web(
            "http://search:8080", "test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            ),
        )


@pytest.mark.asyncio
async def test_oversized_response_rejected():
    with pytest.raises(SearchError, match="too large"):
        await search_web(
            "http://search:8080", "test",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"x" * 1_000_001)
            ),
        )


@pytest.mark.asyncio
async def test_bad_source_urls_are_not_returned():
    result = await search_web(
        "http://search:8080", "test",
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"results": [
                {"title": "Bad", "url": "file:///etc/passwd"},
                {"title": "Bad", "url": "https://user:password@example.org/"},
                {"title": "Good", "url": "https://example.org/"}
            ]}
        )),
    )
    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "Good"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", " ", "x" * 501, None])
async def test_invalid_query_never_sends_request(query):
    def unexpected(request):
        raise AssertionError("No network request expected")
    with pytest.raises(SearchError, match="query"):
        await search_web(
            "http://search:8080", query,
            transport=httpx.MockTransport(unexpected),
        )
