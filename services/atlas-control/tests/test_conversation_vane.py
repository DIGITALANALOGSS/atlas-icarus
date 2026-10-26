import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app import conversation_research as research
from app import conversation_vane as vane
from app.conversation_search import SearchError


def response_data():
    return {
        "message": "Supported answer [1].",
        "sources": [{
            "content": "Untrusted evidence.",
            "metadata": {
                "title": "Example",
                "url": "https://example.org/",
            },
        }],
    }


@pytest.fixture
def configured(monkeypatch):
    values = {
        "ATLAS_RESEARCH_ENABLED": "1",
        "ATLAS_RESEARCH_BACKEND": "vane",
        "ATLAS_VANE_BASE_URL": "http://vane:3000",
        "ATLAS_VANE_CHAT_PROVIDER_ID":
            "11111111-1111-4111-8111-111111111111",
        "ATLAS_VANE_CHAT_MODEL": "test-chat",
        "ATLAS_VANE_EMBEDDING_PROVIDER_ID":
            "22222222-2222-4222-8222-222222222222",
        "ATLAS_VANE_EMBEDDING_MODEL": "test-embedding",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ATLAS_VANE_OPTIMIZATION_MODE", raising=False)
    monkeypatch.delenv("ATLAS_SEARCH_BASE_URL", raising=False)


@pytest.fixture
def wire(monkeypatch):
    real_client = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            vane.httpx, "AsyncClient",
            lambda **kwargs: real_client(
                transport=httpx.MockTransport(handler), **kwargs
            ),
        )
    return install


@pytest.mark.asyncio
async def test_vane_dispatch_and_payload(configured, wire, monkeypatch):
    seen = []

    async def handler(request):
        seen.append(json.loads(request.content))
        assert str(request.url) == "http://vane:3000/api/search"
        return httpx.Response(200, json=response_data())

    wire(handler)
    search = AsyncMock()
    monkeypatch.setattr(research, "search_web", search)
    model = AsyncMock()
    result = await research.research_answer(model, "question", remaining=2)
    assert len(seen) == 1
    assert seen[0]["query"] == "question"
    assert seen[0]["history"] == []
    assert seen[0]["sources"] == ["web"]
    assert seen[0]["stream"] is False
    assert seen[0]["chatModel"]["key"] == "test-chat"
    assert seen[0]["embeddingModel"]["key"] == "test-embedding"
    assert "https://example.org/" in result
    assert "claims not independently verified" in result
    assert "Atlas research elapsed:" in result
    search.assert_not_awaited()
    model.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_backend_remains_searxng(configured, monkeypatch):
    monkeypatch.delenv("ATLAS_RESEARCH_BACKEND")
    monkeypatch.setenv("ATLAS_SEARCH_BASE_URL", "http://search:8080")
    search = AsyncMock(return_value={"results": []})
    monkeypatch.setattr(research, "search_web", search)
    result = await research.research_answer(
        AsyncMock(), "question", remaining=2
    )
    assert "no usable sources" in result
    search.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend,enabled,match", [
    ("vane", "0", "disabled"),
    ("unknown", "1", "unknown research backend"),
])
async def test_rejects_before_network(
    configured, monkeypatch, wire, backend, enabled, match
):
    def forbidden(request):
        pytest.fail("Network transport must not be reached")
    wire(forbidden)
    monkeypatch.setenv("ATLAS_RESEARCH_BACKEND", backend)
    monkeypatch.setenv("ATLAS_RESEARCH_ENABLED", enabled)
    with pytest.raises(SearchError, match=match):
        await research.research_answer(AsyncMock(), "question", remaining=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("ATLAS_VANE_CHAT_PROVIDER_ID", ""),
    ("ATLAS_VANE_EMBEDDING_MODEL", ""),
    ("ATLAS_VANE_BASE_URL", "http://user:secret@vane:3000"),
    ("ATLAS_VANE_OPTIMIZATION_MODE", "unknown"),
])
async def test_bad_configuration_prevents_network(
    configured, monkeypatch, wire, field, value
):
    wire(lambda request: pytest.fail("Unexpected network access"))
    monkeypatch.setenv(field, value)
    with pytest.raises(SearchError):
        await vane.vane_answer("question", remaining=2)


@pytest.mark.parametrize("kind", [
    "empty", "missing_sources", "bad_citation", "missing_citation",
    "unsafe_url", "invalid_source", "invalid_metadata",
])
def test_rejects_bad_answers(kind):
    data = response_data()
    if kind == "empty":
        data["message"] = ""
    elif kind == "missing_sources":
        data["sources"] = []
    elif kind == "bad_citation":
        data["message"] = "Unsupported [2]."
    elif kind == "missing_citation":
        data["message"] = "No source numbers."
    elif kind == "unsafe_url":
        data["sources"][0]["metadata"]["url"] = "javascript:alert(1)"
    elif kind == "invalid_source":
        data["sources"].append(None)
    elif kind == "invalid_metadata":
        data["sources"][0]["metadata"] = None
    with pytest.raises(SearchError):
        vane._answer(data)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 400, 500])
async def test_http_failure_has_no_retry_or_body_leak(
    configured, wire, status
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            headers={"location": "http://other.invalid/"},
            text="PRIVATE-UPSTREAM-DETAIL",
        )

    wire(handler)
    with pytest.raises(SearchError, match=f"HTTP {status}") as error:
        await vane.vane_answer("question", remaining=2)
    assert len(calls) == 1
    assert "PRIVATE-UPSTREAM-DETAIL" not in str(error.value)


@pytest.mark.asyncio
async def test_deadline_closes_client_without_claiming_backend_stop(
    configured, wire
):
    closed = asyncio.Event()

    async def handler(request):
        try:
            await asyncio.sleep(10)
        finally:
            closed.set()

    wire(handler)
    with pytest.raises(SearchError, match="backend cancellation unverified"):
        await vane.vane_answer("question", remaining=0.02)
    assert closed.is_set()


@pytest.mark.asyncio
async def test_caller_cancellation_propagates(configured, wire):
    entered = asyncio.Event()

    async def handler(request):
        entered.set()
        await asyncio.sleep(10)

    wire(handler)
    task = asyncio.create_task(vane.vane_answer("question", remaining=2))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("body,match", [
    (b"not-json", "invalid JSON"),
    (b"x" * (vane.MAX_BYTES + 1), "size limit"),
])
async def test_response_limits(configured, wire, body, match):
    wire(lambda request: httpx.Response(200, content=body))
    with pytest.raises(SearchError, match=match):
        await vane.vane_answer("question", remaining=2)


@pytest.mark.asyncio
async def test_transport_error_does_not_leak_details(configured, wire):
    def handler(request):
        raise httpx.ConnectError("PRIVATE-DETAIL", request=request)

    wire(handler)
    with pytest.raises(SearchError, match="transport failed") as error:
        await vane.vane_answer("question", remaining=2)
    assert "PRIVATE-DETAIL" not in str(error.value)
