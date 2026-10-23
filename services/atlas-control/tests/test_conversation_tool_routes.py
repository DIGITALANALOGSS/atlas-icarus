from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth import Principal
from app import conversation_turn_routes as routes
from app.conversation_adapters import LlamaCppConversationModel
from app.conversation_tool_calls import ToolCapableConversationModel


def actor(jobs=True):
    permissions = {"conversations:read", "conversations:write"}
    if jobs:
        permissions.add("jobs:create")
    return Principal(
        "test-owner", uuid4(), frozenset(permissions), "user"
    )


def setup_route():
    app = FastAPI()
    connection = AsyncMock()
    connection.fetchrow.return_value = {"role": "user"}
    acquire = MagicMock()
    acquire.__aenter__.return_value = connection
    app.state.pool = Mock()
    app.state.pool.acquire.return_value = acquire
    app.state.conversation_model = Mock()
    routes.register_conversation_turn_routes(app, AsyncMock())
    endpoint = next(
        r.endpoint for r in app.routes
        if getattr(r, "path", "") == "/conversations/{conversation_id}/turns"
        and "POST" in getattr(r, "methods", set())
    )
    return app, endpoint


def test_tool_proposals_default_off(monkeypatch):
    monkeypatch.delenv(
        "ATLAS_CONVERSATION_TOOL_PROPOSALS", raising=False
    )
    assert routes.configured_tool_proposals() is False


@pytest.mark.parametrize("value", ["", "true", "yes", "2"])
def test_invalid_tool_setting_rejected(monkeypatch, value):
    monkeypatch.setenv("ATLAS_CONVERSATION_TOOL_PROPOSALS", value)
    with pytest.raises(RuntimeError, match="must be 0 or 1"):
        routes.configured_tool_proposals()


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_factory_selects_expected_adapter_without_requests(
    monkeypatch, enabled
):
    monkeypatch.setenv("ATLAS_CONVERSATION_TOOL_PROPOSALS", enabled)
    monkeypatch.setenv("ATLAS_CONVERSATION_MODEL", "llama_cpp")
    monkeypatch.setenv("ATLAS_MODEL_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("ATLAS_MODEL_TIMEOUT_SECONDS", "60")
    with (
        patch("app.conversation_adapters.urlopen") as legacy,
        patch("httpx.AsyncClient") as http,
    ):
        model = routes.configured_conversation_model()

    expected = (
        ToolCapableConversationModel
        if enabled == "1"
        else LlamaCppConversationModel
    )
    assert type(model) is expected
    legacy.assert_not_called()
    http.assert_not_called()


def test_echo_cannot_enable_proposals(monkeypatch):
    monkeypatch.setenv("ATLAS_CONVERSATION_TOOL_PROPOSALS", "1")
    monkeypatch.setenv("ATLAS_CONVERSATION_MODEL", "echo")
    with pytest.raises(RuntimeError, match="require llama_cpp"):
        routes.configured_conversation_model()


@pytest.mark.parametrize("field", ["tool_mode", "tools"])
def test_request_body_cannot_enable_tools(field):
    with pytest.raises(ValidationError):
        routes.ConversationTurnCreate(
            user_message_id=uuid4(), **{field: True}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", ["0", "1"])
async def test_route_forwards_only_server_selected_mode(
    monkeypatch, enabled
):
    monkeypatch.setenv("ATLAS_CONVERSATION_TOOL_PROPOSALS", enabled)
    app, endpoint = setup_route()
    result = {"turn": {}, "message": {"content": "pending"}}
    with (
        patch.object(
            routes, "expire_conversation_turns",
            new_callable=AsyncMock,
        ),
        patch.object(
            routes, "execute_turn", new_callable=AsyncMock,
            return_value=result,
        ) as execute,
        patch.object(
            routes, "public_turn",
            return_value={"status": "completed"},
        ),
    ):
        await endpoint(
            uuid4(),
            routes.ConversationTurnCreate(user_message_id=uuid4()),
            actor(),
        )
    options = execute.await_args.kwargs
    if enabled == "1":
        assert options["tool_mode"] is True
    else:
        assert "tool_mode" not in options


@pytest.mark.asyncio
async def test_missing_job_permission_rejected_before_database(
    monkeypatch,
):
    monkeypatch.setenv("ATLAS_CONVERSATION_TOOL_PROPOSALS", "1")
    app, endpoint = setup_route()
    with pytest.raises(HTTPException) as caught:
        await endpoint(
            uuid4(),
            routes.ConversationTurnCreate(user_message_id=uuid4()),
            actor(jobs=False),
        )
    assert caught.value.status_code == 403
    app.state.pool.acquire.assert_not_called()


@pytest.mark.asyncio
async def test_incompatible_runtime_model_rejected_before_database(
    monkeypatch,
):
    monkeypatch.setenv("ATLAS_CONVERSATION_TOOL_PROPOSALS", "1")
    app, endpoint = setup_route()
    app.state.conversation_model = object()
    with pytest.raises(HTTPException) as caught:
        await endpoint(
            uuid4(),
            routes.ConversationTurnCreate(user_message_id=uuid4()),
            actor(),
        )
    assert caught.value.status_code == 503
    app.state.pool.acquire.assert_not_called()
