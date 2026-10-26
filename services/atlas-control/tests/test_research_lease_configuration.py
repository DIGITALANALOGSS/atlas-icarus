import pytest
from app.conversation_turn_routes import configured_research_lease


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("ATLAS_RESEARCH_ENABLED", "1")
    monkeypatch.setenv("ATLAS_RESEARCH_LEASE_SECONDS", "300")


@pytest.mark.parametrize("text", [
    "/search question", "/search\nquestion", "  /search question  ",
])
def test_explicit_research_lease(configured, text):
    assert configured_research_lease(text) == 300


@pytest.mark.parametrize("text", [
    "hello", "Discuss /search without running it.",
    "/searching is not the command", "/search", None,
])
def test_ordinary_turn_keeps_default(configured, text):
    assert configured_research_lease(text) is None


def test_disabled_keeps_default(configured, monkeypatch):
    monkeypatch.setenv("ATLAS_RESEARCH_ENABLED", "0")
    assert configured_research_lease("/search question") is None


def test_absent_setting_keeps_default(configured, monkeypatch):
    monkeypatch.delenv("ATLAS_RESEARCH_LEASE_SECONDS")
    assert configured_research_lease("/search question") is None


@pytest.mark.parametrize("value", ["1", "90", "300", "600", " 300 "])
def test_valid_values(configured, monkeypatch, value):
    monkeypatch.setenv("ATLAS_RESEARCH_LEASE_SECONDS", value)
    assert configured_research_lease("/search question") == int(value)


@pytest.mark.parametrize("value", [
    "", "0", "-1", "601", "300.0", "infinity", "999999", "３００",
])
def test_invalid_values(configured, monkeypatch, value):
    monkeypatch.setenv("ATLAS_RESEARCH_LEASE_SECONDS", value)
    with pytest.raises(RuntimeError, match="ATLAS_RESEARCH_LEASE_SECONDS"):
        configured_research_lease("/search question")


# HTTP_RESEARCH_LEASE_FORWARDING_V1
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
from fastapi import FastAPI, HTTPException
from app import conversation_turn_routes as routes


def handler_setup(monkeypatch, text):
    monkeypatch.setenv("ATLAS_CONVERSATION_TOOL_PROPOSALS", "0")
    connection = AsyncMock()
    connection.fetchrow.return_value = {"role": "user", "content": text}
    pool = MagicMock()
    pool.acquire.return_value.__aenter__.return_value = connection
    app = FastAPI()
    app.state.pool = pool
    app.state.conversation_model = object()
    execute = AsyncMock(return_value={"turn": {}, "message": {}})
    monkeypatch.setattr(routes, "execute_turn", execute)
    monkeypatch.setattr(routes, "expire_conversation_turns", AsyncMock())
    monkeypatch.setattr(routes, "public_turn", lambda row: {})
    routes.register_conversation_turn_routes(app, AsyncMock())
    handler = next(
        route.endpoint for route in app.routes
        if route.path == "/conversations/{conversation_id}/turns"
        and "POST" in getattr(route, "methods", set())
    )
    principal = SimpleNamespace(
        tenant_id=uuid4(), subject_id="test-owner", permissions=frozenset()
    )
    request = routes.ConversationTurnCreate(user_message_id=uuid4())
    return handler, principal, request, execute


@pytest.mark.asyncio
@pytest.mark.parametrize("text,enabled,budget,expected", [
    ("/search question", "1", "300", 300),
    ("/search\nquestion", "1", "300", 300),
    ("hello", "1", "300", None),
    ("/search question", "0", "300", None),
    ("/search question", "1", None, None),
])
async def test_handler_forwards_only_selected_lease(
    monkeypatch, text, enabled, budget, expected
):
    monkeypatch.setenv("ATLAS_RESEARCH_ENABLED", enabled)
    if budget is None:
        monkeypatch.delenv("ATLAS_RESEARCH_LEASE_SECONDS", raising=False)
    else:
        monkeypatch.setenv("ATLAS_RESEARCH_LEASE_SECONDS", budget)
    handler, principal, request, execute = handler_setup(monkeypatch, text)
    await handler(uuid4(), request, principal)
    execute.assert_awaited_once()
    kwargs = execute.await_args.kwargs
    if expected is None:
        assert "lease_seconds" not in kwargs
    else:
        assert kwargs["lease_seconds"] == expected


@pytest.mark.asyncio
async def test_invalid_budget_does_not_start_execution(monkeypatch):
    monkeypatch.setenv("ATLAS_RESEARCH_ENABLED", "1")
    monkeypatch.setenv("ATLAS_RESEARCH_LEASE_SECONDS", "601")
    handler, principal, request, execute = handler_setup(
        monkeypatch, "/search question"
    )
    with pytest.raises(HTTPException) as error:
        await handler(uuid4(), request, principal)
    assert error.value.status_code == 503
    execute.assert_not_awaited()
