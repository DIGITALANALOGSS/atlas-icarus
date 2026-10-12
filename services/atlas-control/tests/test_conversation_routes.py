from pathlib import Path
import sys

import httpx
import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from app.main import app

CONVERSATION_ID = "a487f846-e7fd-4081-9d66-006206df885d"


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,expected", [
    ({}, 401),
    ({"Authorization": "Bearer dev-worker"}, 403),
])
@pytest.mark.parametrize("method,path,body", [
    ("POST", "/conversations", {}),
    ("GET", "/conversations", None),
    ("GET", f"/conversations/{CONVERSATION_ID}", None),
    ("GET", f"/conversations/{CONVERSATION_ID}/messages", None),
    ("POST", f"/conversations/{CONVERSATION_ID}/messages", {"content": "Hello"}),
])
async def test_conversation_routes_require_authority(monkeypatch, headers, expected, method, path, body):
    monkeypatch.setattr(app.state, "pool", None, raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.request(method, path, headers=headers, json=body)
    assert response.status_code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"content": ""},
    {"content": "   "},
    {"content": "Hello", "role": "assistant"},
    {"content": "Hello", "role": "system"},
])
async def test_conversation_message_rejects_invalid_input_before_database(monkeypatch, body):
    monkeypatch.setattr(app.state, "pool", None, raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            f"/conversations/{CONVERSATION_ID}/messages",
            headers={"Authorization": "Bearer dev-requester"},
            json=body,
        )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["visible", "missing", "failure"])
async def test_conversation_lookup_scopes_tenant_and_owner(monkeypatch, outcome):
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from uuid import UUID
    from app.auth import DEFAULT_TENANT_ID, development_principals

    calls = []
    conversation_id = UUID(CONVERSATION_ID)
    principal = development_principals()["dev-requester"]
    stamp = datetime(2026, 10, 11, tzinfo=timezone.utc)
    row = {
        "conversation_id": conversation_id,
        "title": "Test conversation",
        "correlation_id": conversation_id,
        "created_at": stamp,
        "updated_at": stamp,
    }

    class Connection:
        async def fetchrow(self, query, *args):
            calls.append((query, args))
            if outcome == "failure":
                raise RuntimeError("synthetic-private-database-detail")
            return row if outcome == "visible" else None

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    monkeypatch.setattr(app.state, "pool", Pool(), raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.get(
            f"/conversations/{CONVERSATION_ID}",
            headers={"Authorization": "Bearer dev-requester"},
        )
    assert response.status_code == {"visible": 200, "missing": 404, "failure": 503}[outcome]
    assert len(calls) == 1
    query, args = calls[0]
    assert "conversation_id = $1" in query
    assert "tenant_id = $2" in query
    assert "owner_subject_id = $3" in query
    assert args == (conversation_id, DEFAULT_TENANT_ID, principal.subject_id)
    assert "synthetic-private-database-detail" not in response.text
    if outcome == "visible":
        body = response.json()
        assert body["conversation_id"] == CONVERSATION_ID
        assert body["title"] == row["title"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["replay", "conflict", "missing"])
async def test_conversation_message_replay_contract(monkeypatch, outcome):
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    from uuid import UUID
    from app.auth import development_principals

    cid = UUID(CONVERSATION_ID)
    client_id = UUID("b487f846-e7fd-4081-9d66-006206df885d")
    message_id = UUID("c487f846-e7fd-4081-9d66-006206df885d")
    principal = development_principals()["dev-requester"]
    calls = []
    events = []
    existing = {
        "message_id": message_id,
        "conversation_id": cid,
        "sequence_number": 1,
        "role": "user",
        "content": "Different" if outcome == "conflict" else "Hello",
        "client_message_id": client_id,
        "created_at": datetime(2026, 10, 11, tzinfo=timezone.utc),
    }

    class Connection:
        @asynccontextmanager
        async def transaction(self):
            events.append("begin")
            try:
                yield
            except BaseException:
                events.append("rollback")
                raise
            else:
                events.append("commit")

        async def fetchrow(self, query, *args):
            calls.append((query, args))
            if len(calls) == 1:
                return None if outcome == "missing" else {"conversation_id": cid}
            return existing

        async def execute(self, query, *args):
            raise AssertionError("Replay must not perform a write")

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

    monkeypatch.setattr(app.state, "pool", Pool(), raising=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(
            f"/conversations/{cid}/messages",
            headers={"Authorization": "Bearer dev-requester"},
            json={"content": "Hello", "client_message_id": str(client_id)},
        )
    assert response.status_code == {"replay": 201, "conflict": 409, "missing": 404}[outcome]
    query, args = calls[0]
    assert "FOR UPDATE" in query
    assert "tenant_id = $2" in query
    assert "owner_subject_id = $3" in query
    assert args == (cid, principal.tenant_id, principal.subject_id)
    assert events == ["begin", "commit" if outcome == "replay" else "rollback"]
    assert len(calls) == (1 if outcome == "missing" else 2)
    if outcome != "missing":
        assert calls[1][1] == (cid, principal.tenant_id, client_id)
        assert "client_message_id = $3" in calls[1][0]
    if outcome == "replay":
        assert response.json()["message_id"] == str(message_id)
