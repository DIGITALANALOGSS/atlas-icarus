import json
import logging
import time

import pytest
from app import conversation_vane as vane
from app.conversation_search import SearchError


def event(kind, data=None):
    return json.dumps(
        {"type": kind, "data": data}, ensure_ascii=False
    ).encode() + b"\n"


SOURCE = {"metadata": {
    "title": "Fixture source", "url": "https://docs.example.org/page/"
}}


def stream_bytes():
    return b"".join([
        event("init"),
        event("sources", [SOURCE]),
        event("response", "Café "),
        event("response", "[1]."),
        event("done"),
    ])


class Response:
    def __init__(self, body, step=7):
        self.body, self.step = body, step

    async def aiter_bytes(self):
        for offset in range(0, len(self.body), self.step):
            yield self.body[offset:offset + self.step]


def test_event_milestones():
    state = vane._new_vane_stream_state()
    assert vane._consume_vane_event(event("init"), state) == ("connected", None)
    assert vane._consume_vane_event(
        event("sources", [SOURCE]), state
    ) == ("sources_received", 1)
    assert vane._consume_vane_event(
        event("response", "Answer "), state
    ) == ("first_answer_text", None)
    assert vane._consume_vane_event(event("response", "[1]."), state) is None
    assert vane._consume_vane_event(event("done"), state) == ("stream_done", None)
    assert "".join(state["parts"]) == "Answer [1]."


@pytest.mark.parametrize("events", [
    [b"not-json"],
    [b"[]"],
    [event("response", "too early")],
    [event("init"), event("init")],
    [event("init"), event("sources", {})],
    [event("init"), event("response", 42)],
    [event("init"), event("unknown")],
    [event("init"), event("done"), event("response", "late")],
    [event("init"), event("error", "upstream failure")],
    [event("init"), event("response", "x" * 65537)],
])
def test_invalid_event_sequences(events):
    state = vane._new_vane_stream_state()
    with pytest.raises(SearchError):
        for line in events:
            vane._consume_vane_event(line, state)


def test_blank_line_is_ignored():
    state = vane._new_vane_stream_state()
    assert vane._consume_vane_event(b" \r\n", state) is None
    assert not state["initialized"]


@pytest.mark.asyncio
@pytest.mark.parametrize("step", [1, 7, 65536])
@pytest.mark.parametrize("final_newline", [True, False])
async def test_fragmented_stream_and_safe_logging(step, final_newline, caplog):
    body = stream_bytes()
    if not final_newline:
        body = body.rstrip(b"\n")
    with caplog.at_level(logging.INFO, logger="app.conversation_vane"):
        data = await vane._read_vane_stream(Response(body, step), time.monotonic())
    assert data["message"] == "Café [1]."
    assert data["sources"] == [SOURCE]
    answer = vane._answer(
        data,
        allowed_source_hosts=("docs.example.org",),
        required_source_urls=("https://docs.example.org/page/",),
    )
    assert "Café [1]." in answer
    assert "sources_received" in caplog.text
    assert "first_answer_text" in caplog.text
    assert "stream_done" in caplog.text
    assert "Café" not in caplog.text
    assert "https://docs.example.org" not in caplog.text


@pytest.mark.asyncio
async def test_missing_completion_is_rejected():
    body = event("init") + event("response", "unfinished")
    with pytest.raises(SearchError, match="without completion"):
        await vane._read_vane_stream(Response(body), time.monotonic())


@pytest.mark.asyncio
async def test_stream_byte_limit(monkeypatch):
    body = stream_bytes()
    monkeypatch.setattr(vane, "MAX_BYTES", len(body) - 1)
    with pytest.raises(SearchError, match="size limit"):
        await vane._read_vane_stream(Response(body), time.monotonic())


@pytest.mark.asyncio
async def test_data_after_completion_is_rejected():
    body = stream_bytes() + event("response", "late")
    with pytest.raises(SearchError, match="after stream completion"):
        await vane._read_vane_stream(Response(body), time.monotonic())


# VANE_STREAM_HTTP_TESTS_V1
import asyncio
import httpx


class HTTPBody(httpx.AsyncByteStream):
    def __init__(self, body, delay=0):
        self.body = body
        self.delay = delay
        self.closed = False
        self.entered = asyncio.Event()

    async def __aiter__(self):
        self.entered.set()
        if self.delay:
            await asyncio.sleep(self.delay)
        for offset in range(0, len(self.body), 7):
            yield self.body[offset:offset + 7]

    async def aclose(self):
        self.closed = True


@pytest.fixture
def http_transport(monkeypatch):
    values = {
        "ATLAS_VANE_BASE_URL": "http://vane:3000",
        "ATLAS_VANE_CHAT_PROVIDER_ID":
            "11111111-1111-4111-8111-111111111111",
        "ATLAS_VANE_CHAT_MODEL": "test-chat",
        "ATLAS_VANE_EMBEDDING_PROVIDER_ID":
            "22222222-2222-4222-8222-222222222222",
        "ATLAS_VANE_EMBEDDING_MODEL": "test-embedding",
        "ATLAS_VANE_OPTIMIZATION_MODE": "speed",
        "ATLAS_VANE_STREAMING": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    real_client = httpx.AsyncClient
    seen = []

    def install(body, delay=0):
        stream = HTTPBody(body, delay)
        async def handler(request):
            seen.append(json.loads(request.content))
            assert str(request.url) == "http://vane:3000/api/search"
            return httpx.Response(200, stream=stream)
        monkeypatch.setattr(
            vane.httpx, "AsyncClient",
            lambda **kwargs: real_client(
                transport=httpx.MockTransport(handler), **kwargs
            ),
        )
        return stream
    return install, seen


@pytest.mark.asyncio
async def test_http_stream_validated_before_return(http_transport):
    install, seen = http_transport
    stream = install(stream_bytes())
    answer = await vane.vane_answer(
        "question", remaining=2,
        allowed_source_hosts=("docs.example.org",),
        required_source_urls=(SOURCE["metadata"]["url"],),
    )
    assert "Café [1]." in answer
    assert len(seen) == 1 and seen[0]["stream"] is True
    assert stream.closed


@pytest.mark.asyncio
async def test_http_stream_cannot_bypass_source_policy(http_transport):
    install, seen = http_transport
    stream = install(stream_bytes())
    with pytest.raises(SearchError, match="outside the allowed hosts"):
        await vane.vane_answer(
            "question", remaining=2,
            allowed_source_hosts=("other.example.org",),
        )
    assert len(seen) == 1
    assert stream.closed


@pytest.mark.asyncio
async def test_http_nonstreaming_remains_default(http_transport, monkeypatch):
    install, seen = http_transport
    monkeypatch.delenv("ATLAS_VANE_STREAMING")
    body = json.dumps({
        "message": "Answer [1].", "sources": [SOURCE]
    }).encode()
    stream = install(body)
    answer = await vane.vane_answer("question", remaining=2)
    assert "Answer [1]." in answer
    assert len(seen) == 1 and seen[0]["stream"] is False
    assert stream.closed


@pytest.mark.asyncio
async def test_invalid_stream_flag_prevents_http(http_transport, monkeypatch):
    install, seen = http_transport
    install(stream_bytes())
    monkeypatch.setenv("ATLAS_VANE_STREAMING", "yes")
    with pytest.raises(SearchError, match="ATLAS_VANE_STREAMING"):
        await vane.vane_answer("question", remaining=2)
    assert seen == []


@pytest.mark.asyncio
async def test_http_stream_timeout_closes_response(http_transport):
    install, seen = http_transport
    stream = install(stream_bytes(), delay=10)
    with pytest.raises(SearchError, match="backend cancellation unverified"):
        await vane.vane_answer("question", remaining=0.02)
    assert len(seen) == 1
    assert stream.closed


@pytest.mark.asyncio
async def test_http_stream_caller_cancellation_closes_response(http_transport):
    install, seen = http_transport
    stream = install(stream_bytes(), delay=10)
    task = asyncio.create_task(vane.vane_answer("question", remaining=2))
    await asyncio.wait_for(stream.entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(seen) == 1
    assert stream.closed


@pytest.mark.asyncio
async def test_http_stream_requires_completion(http_transport):
    install, seen = http_transport
    stream = install(stream_bytes().replace(event("done"), b""))
    with pytest.raises(SearchError, match="without completion"):
        await vane.vane_answer("question", remaining=2)
    assert len(seen) == 1
    assert stream.closed
