import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT / "apps/atlas-workspace"))

import app.conversation_turn_routes as routes
import workspace_context
import workspace_store


@pytest.mark.parametrize("selection", ["omitted", "personal", "project"])
def test_bridge_preserves_workspace_selection(tmp_path, monkeypatch, selection):
    monkeypatch.setattr(workspace_store, "DB", tmp_path / "workspace.sqlite3")
    project = workspace_store.operate(
        "POST", "projects", {"name": "Connected verification"}
    )["id"]

    body = {"user_message_id": str(uuid4())}
    if selection == "personal":
        body["workspace_project_id"] = None
    elif selection == "project":
        body["workspace_project_id"] = project

    result = workspace_context.prepare_turn_body(
        f"/conversations/{uuid4()}/turns", body
    )

    assert result["user_message_id"] == body["user_message_id"]
    assert isinstance(result["memory_context"], list)
    if selection == "omitted":
        assert "workspace_project_id" not in result
    else:
        assert result["workspace_project_id"] == body["workspace_project_id"]


class RouteConnection:
    async def fetchrow(self, sql, *args):
        assert "SELECT message.role" in sql
        return {"role": "user"}


class RoutePool:
    @asynccontextmanager
    async def acquire(self):
        yield RouteConnection()


@pytest.fixture
def endpoint_rig(monkeypatch):
    app = FastAPI()
    app.state.pool = RoutePool()
    app.state.conversation_model = object()

    principal = SimpleNamespace(
        tenant_id=uuid4(),
        subject_id="connected-boundary-owner",
        permissions={"conversations:read", "conversations:write"},
    )
    cid, mid = uuid4(), uuid4()
    turn = {
        "turn_id": uuid4(),
        "conversation_id": cid,
        "user_message_id": mid,
        "assistant_message_id": uuid4(),
        "status": "completed",
        "attempt": 1,
        "error_code": None,
        "correlation_id": uuid4(),
        "created_at": None,
        "started_at": None,
        "completed_at": None,
    }
    execute = AsyncMock(return_value={
        "turn": turn,
        "message": {"content": "Controlled verification response"},
    })
    expire = AsyncMock()
    monkeypatch.setattr(routes, "execute_turn", execute)
    monkeypatch.setattr(routes, "expire_conversation_turns", expire)
    routes.register_conversation_turn_routes(app, AsyncMock())

    post = next(
        route for route in app.routes
        if getattr(route, "path", None)
        == "/conversations/{conversation_id}/turns"
        and "POST" in getattr(route, "methods", set())
    )
    writer = next(
        dependency.call
        for dependency in post.dependant.dependencies
        if dependency.name == "principal"
    )

    async def authorized_test_principal():
        return principal

    app.dependency_overrides[writer] = authorized_test_principal
    return SimpleNamespace(
        app=app, cid=cid, mid=mid, execute=execute, expire=expire,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["omitted", "personal", "project"])
async def test_http_route_forwards_workspace_selection(endpoint_rig, selection):
    rig = endpoint_rig
    body = {"user_message_id": str(rig.mid), "memory_context": ["Saved fact"]}
    project = uuid4()
    if selection == "personal":
        body["workspace_project_id"] = None
    elif selection == "project":
        body["workspace_project_id"] = str(project)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app),
        base_url="http://verification",
    ) as client:
        response = await client.post(
            f"/conversations/{rig.cid}/turns", json=body
        )

    assert response.status_code == 200, response.text
    rig.execute.assert_awaited_once()
    kwargs = rig.execute.await_args.kwargs
    assert kwargs["memory_context"] == ["Saved fact"]
    if selection == "omitted":
        assert "workspace_project_id" not in kwargs
    elif selection == "personal":
        assert kwargs["workspace_project_id"] is None
    else:
        assert kwargs["workspace_project_id"] == project


@pytest.mark.asyncio
async def test_http_rejects_invalid_workspace_before_execution(endpoint_rig):
    rig = endpoint_rig
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app),
        base_url="http://verification",
    ) as client:
        response = await client.post(
            f"/conversations/{rig.cid}/turns",
            json={
                "user_message_id": str(rig.mid),
                "workspace_project_id": "not-a-uuid",
            },
        )
    assert response.status_code == 422, response.text
    rig.execute.assert_not_awaited()
    rig.expire.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["omitted", "personal", "project"])
async def test_bridge_output_passes_through_http(
    endpoint_rig, tmp_path, monkeypatch, selection
):
    rig = endpoint_rig
    monkeypatch.setattr(workspace_store, "DB", tmp_path / "workspace.sqlite3")
    project = workspace_store.operate(
        "POST", "projects", {"name": "Bridge HTTP verification"}
    )["id"]
    body = {"user_message_id": str(rig.mid)}
    if selection == "personal":
        body["workspace_project_id"] = None
    elif selection == "project":
        body["workspace_project_id"] = project

    prepared = workspace_context.prepare_turn_body(
        f"/conversations/{rig.cid}/turns", body
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=rig.app),
        base_url="http://verification",
    ) as client:
        response = await client.post(
            f"/conversations/{rig.cid}/turns", json=prepared
        )

    assert response.status_code == 200, response.text
    kwargs = rig.execute.await_args.kwargs
    assert kwargs["memory_context"] == prepared["memory_context"]
    if selection == "omitted":
        assert "workspace_project_id" not in kwargs
    elif selection == "personal":
        assert kwargs["workspace_project_id"] is None
    else:
        assert str(kwargs["workspace_project_id"]) == project
