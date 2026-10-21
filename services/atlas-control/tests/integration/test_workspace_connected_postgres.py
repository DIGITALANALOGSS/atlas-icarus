import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio

from app.auth import development_principals
from app.conversation_coordinator import reserve_turn
from app.conversation_memory import stored_memory_context
from app.main import app, write_event


class BoundPool:
    def __init__(self, connection):
        self.connection = connection
        self.in_use = 0

    @asynccontextmanager
    async def acquire(self):
        self.in_use += 1
        try:
            yield self.connection
        finally:
            self.in_use -= 1


@pytest_asyncio.fixture
async def workspace_case(monkeypatch):
    database = urlparse(os.environ.get("DATABASE_URL", ""))
    if database.hostname != "postgres-test" or database.path != "/atlas_test":
        pytest.fail("Requires isolated postgres-test/atlas_test")

    monkeypatch.setenv("ATLAS_CONVERSATION_MODEL", "echo")
    principal = development_principals()["dev-requester"]
    cid, user = uuid4(), uuid4()

    async with app.router.lifespan_context(app):
        real_pool = app.state.pool
        original_model = app.state.conversation_model
        async with real_pool.acquire() as connection:
            assert await connection.fetchval(
                "SELECT current_database()"
            ) == "atlas_test"

            binding_table = await connection.fetchval(
                "SELECT to_regclass('conversation_workspace_bindings')"
            )
            snapshot_column = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'conversation_turns'
                      AND column_name = 'memory_context_sha256'
                )
                """
            )
            if binding_table is None or not snapshot_column:
                pytest.fail(
                    "Isolated database schema lacks migration 017 or 018. "
                    "Do not apply migrations to the normal Atlas database."
                )

            transaction = connection.transaction()
            await transaction.start()
            try:
                await connection.execute(
                    "INSERT INTO conversations "
                    "(conversation_id,tenant_id,owner_subject_id,correlation_id) "
                    "VALUES ($1,$2,$3,$1)",
                    cid, principal.tenant_id, principal.subject_id,
                )
                await connection.execute(
                    "INSERT INTO conversation_messages "
                    "(message_id,tenant_id,conversation_id,sequence_number,role,content) "
                    "VALUES ($1,$2,$3,1,'user','Connected PostgreSQL verification')",
                    user, principal.tenant_id, cid,
                )
                pool = BoundPool(connection)
                app.state.pool = pool
                yield {
                    "connection": connection,
                    "pool": pool,
                    "principal": principal,
                    "cid": cid,
                    "user": user,
                }
            finally:
                app.state.pool = real_pool
                app.state.conversation_model = original_model
                await transaction.rollback()


async def binding_count(case):
    return await case["connection"].fetchval(
        "SELECT count(*) FROM conversation_workspace_bindings "
        "WHERE tenant_id=$1 AND conversation_id=$2",
        case["principal"].tenant_id, case["cid"],
    )


async def binding_event_count(case):
    return await case["connection"].fetchval(
        "SELECT count(*) FROM events "
        "WHERE correlation_id=$1 "
        "AND event_type='governance.conversation.workspace.bound'",
        case["cid"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["omitted", "personal", "project"])
async def test_http_workspace_execution_and_replay(workspace_case, selection):
    case = workspace_case
    calls = []

    class ControlledModel:
        async def complete(self, messages):
            assert case["pool"].in_use == 0
            calls.append(messages)
            assert messages[0].role == "system"
            assert "Verified saved preference" in messages[0].content
            assert messages[-1].role == "user"
            assert messages[-1].content == "Connected PostgreSQL verification"
            return "Connected PostgreSQL response"

    app.state.conversation_model = ControlledModel()
    project = uuid4()
    body = {
        "user_message_id": str(case["user"]),
        "memory_context": ["Verified saved preference"],
    }
    if selection == "personal":
        body["workspace_project_id"] = None
    elif selection == "project":
        body["workspace_project_id"] = str(project)

    headers = {"Authorization": "Bearer dev-requester"}
    path = f"/conversations/{case['cid']}/turns"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.post(path, headers=headers, json=body)
        assert response.status_code == 200, response.text
        assert response.json()["message"]["content"] == (
            "Connected PostgreSQL response"
        )

        replay = await client.post(path, headers=headers, json=body)
        assert replay.status_code == 200, replay.text
        assert replay.json() == response.json()
        assert len(calls) == 1

        if selection != "omitted":
            conflict = dict(body)
            conflict["workspace_project_id"] = (
                str(uuid4()) if selection == "personal" else None
            )
            rejected = await client.post(path, headers=headers, json=conflict)
            assert rejected.status_code == 409, rejected.text
            assert len(calls) == 1

    connection = case["connection"]
    turn = await connection.fetchrow(
        "SELECT * FROM conversation_turns "
        "WHERE tenant_id=$1 AND conversation_id=$2 AND user_message_id=$3",
        case["principal"].tenant_id, case["cid"], case["user"],
    )
    assert turn["status"] == "completed"
    assert turn["turn_id"] == UUID(response.json()["turn"]["turn_id"])
    assert stored_memory_context(dict(turn)) == ["Verified saved preference"]

    expected = 0 if selection == "omitted" else 1
    assert await binding_count(case) == expected
    assert await binding_event_count(case) == expected

    if expected:
        binding = await connection.fetchrow(
            "SELECT scope_type,workspace_project_id,bound_by_subject_id "
            "FROM conversation_workspace_bindings "
            "WHERE tenant_id=$1 AND conversation_id=$2",
            case["principal"].tenant_id, case["cid"],
        )
        assert binding["scope_type"] == selection
        assert binding["workspace_project_id"] == (
            project if selection == "project" else None
        )
        assert binding["bound_by_subject_id"] == case["principal"].subject_id

    assert await connection.fetchval(
        "SELECT count(*) FROM conversation_messages "
        "WHERE tenant_id=$1 AND conversation_id=$2 AND role='assistant'",
        case["principal"].tenant_id, case["cid"],
    ) == 1

    for event_type in (
        "governance.conversation.turn.queued",
        "governance.conversation.turn.completed",
    ):
        assert await connection.fetchval(
            "SELECT count(*) FROM events "
            "WHERE correlation_id=$1 AND event_type=$2",
            turn["correlation_id"], event_type,
        ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["binding_event", "queued_event"])
async def test_reservation_failure_rolls_back_binding_and_events(
    workspace_case, failure_point
):
    case = workspace_case
    queued_correlations = []

    async def failing_event(connection, **fields):
        await write_event(connection, **fields)
        event_type = fields["event_type"]
        if event_type == "governance.conversation.turn.queued":
            queued_correlations.append(fields["correlation_id"])
            assert await binding_count(case) == 1
            assert await binding_event_count(case) == 1
        if (
            failure_point == "binding_event"
            and event_type == "governance.conversation.workspace.bound"
        ) or (
            failure_point == "queued_event"
            and event_type == "governance.conversation.turn.queued"
        ):
            raise RuntimeError("injected workspace reservation audit failure")

    with pytest.raises(RuntimeError, match="injected workspace reservation"):
        await reserve_turn(
            case["pool"], case["principal"], case["cid"], case["user"],
            write_event=failing_event,
            workspace_project_id=uuid4(),
            memory_context=["Rollback verification preference"],
        )

    assert await binding_count(case) == 0
    assert await binding_event_count(case) == 0
    assert await case["connection"].fetchval(
        "SELECT count(*) FROM conversation_turns "
        "WHERE tenant_id=$1 AND conversation_id=$2",
        case["principal"].tenant_id, case["cid"],
    ) == 0

    for correlation in queued_correlations:
        assert await case["connection"].fetchval(
            "SELECT count(*) FROM events WHERE correlation_id=$1",
            correlation,
        ) == 0

    if failure_point == "queued_event":
        assert len(queued_correlations) == 1
