from pathlib import Path
import json
import sys
from unittest.mock import patch

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from app.conversation_adapters import (
    ConversationModelError,
    ConversationTurn,
    EchoConversationModel,
    LlamaCppConversationModel,
)


@pytest.mark.asyncio
async def test_echo_model_returns_latest_user_turn() -> None:
    model = EchoConversationModel()

    reply = await model.complete(
        [
            ConversationTurn(role="user", content="First request"),
            ConversationTurn(role="assistant", content="First reply"),
            ConversationTurn(role="user", content="Latest request"),
        ]
    )

    assert reply == "Atlas received: Latest request"


@pytest.mark.asyncio
async def test_echo_model_handles_empty_conversation() -> None:
    model = EchoConversationModel()

    reply = await model.complete([])

    assert reply == "Atlas is ready."


@pytest.mark.asyncio
async def test_echo_model_ignores_non_user_turns() -> None:
    model = EchoConversationModel()

    reply = await model.complete(
        [ConversationTurn(role="assistant", content="Prior reply")]
    )

    assert reply == "Atlas is ready."


@pytest.mark.asyncio
async def test_llama_cpp_model_posts_ordered_messages_and_returns_content() -> None:
    captured: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "  Generated reply  "}}]}
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer test-key"
        assert request.get_header("Content-type") == "application/json"
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    model = LlamaCppConversationModel(
        base_url="http://127.0.0.1:8080/v1/",
        model="atlas-qwen",
        api_key="test-key",
        timeout_seconds=12,
    )

    with patch("app.conversation_adapters.urlopen", fake_urlopen):
        reply = await model.complete(
            [
                ConversationTurn(role="system", content="Be concise."),
                ConversationTurn(role="user", content="Hello"),
            ]
        )

    assert reply == "Generated reply"
    assert captured["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    assert captured["body"] == {
        "model": "atlas-qwen",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Hello"},
        ],
        "temperature": 0.2,
        "max_tokens": 512,
        "stream": False,
    }
    assert captured["timeout"] == 12


def test_llama_cpp_model_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("ATLAS_MODEL_API_KEY", raising=False)

    with pytest.raises(
        ConversationModelError,
        match="ATLAS_MODEL_API_KEY is not configured",
    ):
        LlamaCppConversationModel.from_environment()


@pytest.mark.parametrize("value", ["bad", "0", "-1", "nan", "inf", "-inf"])
def test_model_invalid_timeout(monkeypatch, value):
    monkeypatch.setenv("ATLAS_MODEL_API_KEY", "test-key")
    monkeypatch.setenv("ATLAS_MODEL_TIMEOUT_SECONDS", value)
    with pytest.raises(ConversationModelError):
        LlamaCppConversationModel.from_environment()


def test_model_environment_defaults_without_request(monkeypatch):
    for name in ("ATLAS_MODEL_BASE_URL", "ATLAS_MODEL_NAME", "ATLAS_MODEL_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ATLAS_MODEL_API_KEY", "test-key")
    with patch("app.conversation_adapters.urlopen") as transport:
        model = LlamaCppConversationModel.from_environment()
        transport.assert_not_called()
    assert model.base_url == "http://127.0.0.1:8080/v1"
    assert model.model == "atlas-qwen"
    assert model.timeout_seconds == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    b"not-json", b"{}", b"{\"choices\":[]}",
    b"{\"choices\":[{\"message\":{}}]}",
    b"{\"choices\":[{\"message\":{\"content\":null}}]}",
    b"{\"choices\":[{\"message\":{\"content\":123}}]}",
    b"{\"choices\":[{\"message\":{\"content\":\"   \"}}]}",
])
async def test_model_rejects_invalid_response(raw):
    model = LlamaCppConversationModel(
        base_url="http://127.0.0.1:8080/v1",
        model="atlas-qwen", api_key="test-key",
    )
    with patch("app.conversation_adapters.urlopen") as transport:
        transport.return_value.__enter__.return_value.read.return_value = raw
        with pytest.raises(ConversationModelError):
            await model.complete([ConversationTurn(role="user", content="Hello")])


@pytest.mark.parametrize("value", [None, "", "   "])
def test_model_constructor_rejects_missing_key(value):
    with patch("app.conversation_adapters.urlopen") as transport:
        with pytest.raises(ConversationModelError):
            LlamaCppConversationModel(
                base_url="http://127.0.0.1:8080/v1",
                model="atlas-qwen", api_key=value,
            )
        transport.assert_not_called()


@pytest.mark.parametrize("value", [None, "bad", 0, -1, float("nan"), float("inf")])
def test_model_constructor_rejects_invalid_timeout(value):
    with pytest.raises(ConversationModelError):
        LlamaCppConversationModel(
            base_url="http://127.0.0.1:8080/v1",
            model="atlas-qwen", api_key="test-key", timeout_seconds=value,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "url", "os"])
async def test_model_transport_failure(kind):
    from io import BytesIO
    from urllib.error import HTTPError, URLError

    errors = {
        "http": HTTPError(
            "http://127.0.0.1:8080/v1/chat/completions",
            503, "Unavailable", {}, BytesIO(b"provider unavailable"),
        ),
        "url": URLError("connection refused"),
        "os": OSError("transport unavailable"),
    }
    expected = {
        "http": "HTTP 503",
        "url": "network connection error",
        "os": "transport error",
    }
    model = LlamaCppConversationModel(
        base_url="http://127.0.0.1:8080/v1",
        model="atlas-qwen", api_key="test-key",
    )
    with patch("app.conversation_adapters.urlopen", side_effect=errors[kind]) as transport:
        with pytest.raises(ConversationModelError, match=expected[kind]):
            await model.complete([ConversationTurn(role="user", content="Hello")])
        transport.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "url", "os"])
async def test_model_errors_omit_provider_details(kind):
    from io import BytesIO
    from urllib.error import HTTPError, URLError

    sensitive = "synthetic-private-provider-detail"
    errors = {
        "http": HTTPError(
            "http://127.0.0.1:8080/v1/chat/completions",
            500, sensitive, {}, BytesIO(sensitive.encode()),
        ),
        "url": URLError(sensitive),
        "os": OSError(sensitive),
    }
    model = LlamaCppConversationModel(
        base_url="http://127.0.0.1:8080/v1",
        model="atlas-qwen", api_key="test-key",
    )
    with patch("app.conversation_adapters.urlopen", side_effect=errors[kind]):
        with pytest.raises(ConversationModelError) as caught:
            await model.complete([ConversationTurn(role="user", content="Hello")])
    assert sensitive not in str(caught.value)


@pytest.mark.asyncio
async def test_model_invalid_response_encoding():
    model = LlamaCppConversationModel(
        base_url="http://127.0.0.1:8080/v1",
        model="atlas-qwen", api_key="test-key",
    )
    with patch("app.conversation_adapters.urlopen") as transport:
        transport.return_value.__enter__.return_value.read.return_value = b"\xff"
        with pytest.raises(ConversationModelError):
            await model.complete([ConversationTurn(role="user", content="Hello")])


@pytest.mark.asyncio
async def test_model_cancelled_caller_does_not_receive_late_reply():
    import asyncio
    import threading

    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked_completion(self, payload):
        entered.set()
        try:
            if not release.wait(5):
                raise RuntimeError("Test worker release timed out")
            return "Late reply"
        finally:
            finished.set()

    model = LlamaCppConversationModel(
        base_url="http://127.0.0.1:8080/v1",
        model="atlas-qwen", api_key="test-key",
    )
    with patch.object(LlamaCppConversationModel, "_complete_sync", blocked_completion):
        task = asyncio.create_task(
            model.complete([ConversationTurn(role="user", content="Hello")])
        )
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert task.cancelled()
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert await asyncio.to_thread(finished.wait, 3)


def test_model_construction_does_not_request_completion():
    with patch("app.conversation_adapters.urlopen") as transport:
        model = LlamaCppConversationModel(
            base_url="http://127.0.0.1:8080/v1",
            model="atlas-qwen", api_key="test-key", timeout_seconds="12",
        )
        assert model.timeout_seconds == 12.0
        transport.assert_not_called()


@pytest.mark.asyncio
async def test_model_transport_timeout_is_explicit_and_not_retried():
    model = LlamaCppConversationModel(
        base_url="http://127.0.0.1:8080/v1",
        model="atlas-qwen", api_key="test-key",
    )
    with patch(
        "app.conversation_adapters.urlopen",
        side_effect=TimeoutError("synthetic-private-timeout-detail"),
    ) as transport:
        with pytest.raises(ConversationModelError, match="transport error") as caught:
            await model.complete([ConversationTurn(role="user", content="Hello")])
        transport.assert_called_once()
    assert "synthetic-private-timeout-detail" not in str(caught.value)


def test_model_representation_omits_api_key():
    key = "synthetic-private-key-for-repr-test"
    model = LlamaCppConversationModel(
        base_url="http://127.0.0.1:8080/v1",
        model="atlas-qwen", api_key=key,
    )
    assert key not in repr(model)
    assert "api_key=" not in repr(model)
    assert key not in str(model)
