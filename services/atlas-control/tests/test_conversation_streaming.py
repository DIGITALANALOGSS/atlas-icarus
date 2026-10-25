import asyncio
import json
import sys
from contextlib import aclosing
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.conversation_adapters import (
    ConversationModelError,
    ConversationTurn,
    LlamaCppConversationModel,
)
from app.conversation_streaming import decode_event


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, block=False, done=True):
        self.block = block
        self.done = done
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aiter__(self):
        yield b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()
        if self.done:
            yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


def make_model(client=None):
    return LlamaCppConversationModel(
        base_url="http://model.test/v1",
        model="test-model",
        api_key="test-key",
        timeout_seconds=5,
        stream_client=client,
    )


@pytest.mark.parametrize("data", [
    "not-json",
    '{"error":{"message":"failed"}}',
    '{"choices":[{"delta":{"content":123}}]}',
])
def test_decode_rejects_invalid_events(data):
    with pytest.raises(ValueError):
        decode_event(data)


def test_decode_completion_marker():
    assert decode_event("[DONE]") == (True, "")


@pytest.mark.asyncio
async def test_shared_client_reused_and_responses_closed():
    bodies = []

    def handler(request):
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert request.headers["authorization"] == "Bearer test-key"
        body = TrackingStream()
        bodies.append(body)
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        model = make_model(client)
        for _ in range(2):
            result = [
                text async for text in model.stream(
                    [ConversationTurn("user", "Hi")]
                )
            ]
            assert result == ["Hello"]
            assert not client.is_closed
        assert len(bodies) == 2
        assert all(body.closed for body in bodies)
    finally:
        await client.aclose()
    assert client.is_closed


@pytest.mark.asyncio
async def test_cancellation_closes_response_not_shared_client():
    body = TrackingStream(block=True)

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        async def consume():
            async with aclosing(make_model(client).stream([])) as stream:
                async for _ in stream:
                    pass

        task = asyncio.create_task(consume())
        try:
            await asyncio.wait_for(body.waiting.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert body.closed
            assert not client.is_closed
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


@pytest.mark.asyncio
async def test_early_close_preserves_shared_client():
    body = TrackingStream(block=True)

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        async with aclosing(make_model(client).stream([])) as stream:
            assert await anext(stream) == "Hello"
        assert body.closed
        assert not client.is_closed


@pytest.mark.asyncio
async def test_missing_completion_marker_is_error():
    body = TrackingStream(done=False)

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(
            ConversationModelError, match="without a completion marker"
        ):
            async for _ in make_model(client).stream([]):
                pass
        assert body.closed
        assert not client.is_closed


@pytest.mark.asyncio
async def test_standalone_fallback_client_is_closed(monkeypatch):
    real_client = httpx.AsyncClient
    created = []

    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=TrackingStream(),
        )

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        client = real_client(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    assert [
        text async for text in make_model().stream([])
    ] == ["Hello"]
    assert len(created) == 1
    assert created[0].is_closed
