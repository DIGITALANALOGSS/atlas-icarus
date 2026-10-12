import asyncio
import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.auth import development_principals
from app.conversation_coordinator import (
    ConversationCoordinatorError, claim_turn, execute_turn,
    expire_conversation_turns, publish_turn, reserve_turn,
)
from app.main import app, write_event


@pytest_asyncio.fixture
async def conversation_case(monkeypatch):
    database = urlparse(os.environ.get("DATABASE_URL", ""))
    if database.hostname != "postgres-test" or database.path != "/atlas_test":
        pytest.fail("Requires the isolated postgres-test database")
    monkeypatch.setenv("ATLAS_CONVERSATION_MODEL", "echo")
    principal = development_principals()["dev-requester"]
    cid, user = uuid4(), uuid4()
    async with app.router.lifespan_context(app):
        pool = app.state.pool
        try:
            async with pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO conversations "
                        "(conversation_id,tenant_id,owner_subject_id,correlation_id) "
                        "VALUES ($1,$2,$3,$1)",
                        cid, principal.tenant_id, principal.subject_id,
                    )
                    await connection.execute(
                        "INSERT INTO conversation_messages "
                        "(message_id,tenant_id,conversation_id,sequence_number,role,content) "
                        "VALUES ($1,$2,$3,1,'user','First')",
                        user, principal.tenant_id, cid,
                    )
            yield pool, principal, cid, user
        finally:
            async with pool.acquire() as connection:
                async with connection.transaction():
                    for table in (
                        "conversation_turns", "conversation_messages", "conversations",
                    ):
                        await connection.execute(
                            "DELETE FROM " + table +
                            " WHERE conversation_id=$1 AND tenant_id=$2",
                            cid, principal.tenant_id,
                        )


class BoundPool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.asyncio
async def test_separate_connection_claim_contention(conversation_case):
    pool, principal, cid, user = conversation_case
    reserved = await reserve_turn(pool, principal, cid, user, write_event=write_event)
    async with pool.acquire() as first, pool.acquire() as second:
        assert await first.fetchval("SELECT pg_backend_pid()") != (
            await second.fetchval("SELECT pg_backend_pid()")
        )
        gate = asyncio.Event()

        async def contender(connection):
            await gate.wait()
            return await claim_turn(
                BoundPool(connection), principal, reserved["turn_id"],
                write_event=write_event,
            )

        tasks = [
            asyncio.create_task(contender(first)),
            asyncio.create_task(contender(second)),
        ]
        gate.set()
        outcomes = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=10,
        )
    winners = [item for item in outcomes if isinstance(item, dict)]
    losers = [
        item for item in outcomes if isinstance(item, ConversationCoordinatorError)
    ]
    assert len(winners) == 1, outcomes
    assert len(losers) == 1, outcomes
    async with pool.acquire() as connection:
        stored = await connection.fetchrow(
            "SELECT * FROM conversation_turns WHERE turn_id=$1", reserved["turn_id"],
        )
        assert stored["attempt"] == 1
        assert stored["worker_token"] == winners[0]["worker_token"]
        assert await connection.fetchval(
            "SELECT count(*) FROM events WHERE correlation_id=$1 "
            "AND event_type='governance.conversation.turn.running'",
            reserved["correlation_id"],
        ) == 1


@pytest.mark.asyncio
async def test_cancellation_during_generation_blocks_publication(
    conversation_case, monkeypatch,
):
    pool, principal, cid, user = conversation_case
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    class BlockingModel:
        async def complete(self, messages):
            calls.append(messages)
            assert [(m.role, m.content) for m in messages] == [("user", "First")]
            entered.set()
            await release.wait()
            return "Late assistant reply"

    monkeypatch.setattr(app.state, "conversation_model", BlockingModel())
    headers = {"Authorization": "Bearer dev-requester"}
    path = "/conversations/" + str(cid) + "/turns"
    body = {"user_message_id": str(user)}
    task = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        try:
            task = asyncio.create_task(client.post(path, headers=headers, json=body))
            await asyncio.wait_for(entered.wait(), timeout=5)
            duplicate = await client.post(path, headers=headers, json=body)
            assert duplicate.status_code == 409, duplicate.text
            assert len(calls) == 1
            later = await client.post(
                "/conversations/" + str(cid) + "/messages",
                headers=headers, json={"content": "Later user message"},
            )
            assert later.status_code == 201, later.text
            async with pool.acquire() as connection:
                running = await connection.fetchrow(
                    "SELECT * FROM conversation_turns WHERE conversation_id=$1", cid,
                )
            assert running["status"] == "running"
            cancel_path = path + "/" + str(running["turn_id"]) + "/cancel"
            cancelled = await client.post(cancel_path, headers=headers)
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelled"
            release.set()
            response = await asyncio.wait_for(task, timeout=5)
            assert response.status_code == 409, response.text
        finally:
            release.set()
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    async with pool.acquire() as connection:
        assert await connection.fetchval(
            "SELECT status FROM conversation_turns WHERE conversation_id=$1", cid,
        ) == "cancelled"
        assert await connection.fetchval(
            "SELECT count(*) FROM conversation_messages "
            "WHERE conversation_id=$1 AND role='assistant'", cid,
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM events WHERE correlation_id=$1 "
            "AND event_type='governance.conversation.turn.completed'",
            running["correlation_id"],
        ) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["timeout", "interruption"])
async def test_abandoned_execution_expires_without_publication(
    conversation_case, scenario,
):
    pool, principal, cid, user = conversation_case
    entered = asyncio.Event()

    class WaitingModel:
        async def complete(self, messages):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(execute_turn(
        pool, principal, cid, user,
        model=WaitingModel(), write_event=write_event,
        lease_seconds=1 if scenario == "timeout" else 90,
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        async with pool.acquire() as connection:
            claimed_token = await connection.fetchval(
                "SELECT worker_token FROM conversation_turns WHERE conversation_id=$1",
                cid,
            )
        assert claimed_token is not None
        if scenario == "interruption":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            assert task.done(), "Outer watchdog expired before execution completed"
            assert not task.cancelled()
            assert isinstance(task.exception(), TimeoutError)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async with pool.acquire() as connection:
        abandoned = await connection.fetchrow(
            "SELECT * FROM conversation_turns WHERE conversation_id=$1", cid,
        )
        if scenario == "interruption":
            assert abandoned["status"] == "running"
        else:
            assert abandoned["status"] in {"running", "failed"}

        needs_expiry = abandoned["status"] == "running"
        if needs_expiry:
            await connection.execute(
                "UPDATE conversation_turns SET lease_expires_at = "
                "clock_timestamp() - INTERVAL '1 second' WHERE turn_id=$1",
                abandoned["turn_id"],
            )
        else:
            assert abandoned["error_code"] == "execution_timeout"
            assert abandoned["completed_at"] is not None
            assert abandoned["worker_token"] is None
            assert abandoned["lease_expires_at"] is None

    expired = await expire_conversation_turns(
        pool, principal, cid, write_event=write_event,
    )
    if needs_expiry:
        assert len(expired) == 1
        assert expired[0]["status"] == "failed"
        assert expired[0]["error_code"] == "lease_expired"
        assert expired[0]["worker_token"] is None
        assert expired[0]["lease_expires_at"] is None
    else:
        assert expired == []
    assert await expire_conversation_turns(
        pool, principal, cid, write_event=write_event,
    ) == []
    with pytest.raises(ConversationCoordinatorError):
        await publish_turn(
            pool, principal, abandoned["turn_id"],
            worker_token=claimed_token,
            content="Stale reply", write_event=write_event,
        )
    async with pool.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM conversation_messages "
            "WHERE conversation_id=$1 AND role='assistant'", cid,
        ) == 0
        assert await connection.fetchval(
            "SELECT count(*) FROM events WHERE correlation_id=$1 "
            "AND event_type='governance.conversation.turn.failed'",
            abandoned["correlation_id"],
        ) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["same_message_retry", "queued_cancel"])
async def test_reserved_unclaimed_turn_recovery(conversation_case, recovery):
    pool, principal, cid, user = conversation_case
    reserved = await reserve_turn(
        pool, principal, cid, user, write_event=write_event,
    )
    assert reserved["status"] == "queued"
    assert reserved["attempt"] == 0
    assert reserved["worker_token"] is None
    assert reserved["lease_expires_at"] is None

    headers = {"Authorization": "Bearer dev-requester"}
    detail = f"/conversations/{cid}/turns/{reserved['turn_id']}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        response = await client.get(detail, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "queued"

        later = await client.post(
            f"/conversations/{cid}/messages",
            headers=headers, json={"content": "Second"},
        )
        assert later.status_code == 201, later.text
        from uuid import UUID
        later_user = UUID(later.json()["message_id"])

        with pytest.raises(
            ConversationCoordinatorError, match="conversation has an active turn",
        ):
            await reserve_turn(
                pool, principal, cid, later_user, write_event=write_event,
            )

        if recovery == "same_message_retry":
            calls = []

            class Model:
                async def complete(self, messages):
                    calls.append(messages)
                    assert [(m.role, m.content) for m in messages] == [
                        ("user", "First"),
                    ]
                    return "Recovered reply"

            model = Model()
            result = await execute_turn(
                pool, principal, cid, user,
                model=model, write_event=write_event,
            )
            assert result["turn"]["turn_id"] == reserved["turn_id"]
            assert result["turn"]["status"] == "completed"
            assert result["turn"]["attempt"] == 1
            assert result["message"]["content"] == "Recovered reply"

            replay = await execute_turn(
                pool, principal, cid, user,
                model=model, write_event=write_event,
            )
            assert replay == result
            assert len(calls) == 1
        else:
            cancelled = await client.post(detail + "/cancel", headers=headers)
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelled"
            assert cancelled.json()["attempt"] == 0

            repeated = await client.post(detail + "/cancel", headers=headers)
            assert repeated.status_code == 200, repeated.text
            assert repeated.json() == cancelled.json()

            async with pool.acquire() as connection:
                assert await connection.fetchval(
                    "SELECT count(*) FROM conversation_messages "
                    "WHERE conversation_id=$1 AND role='assistant'", cid,
                ) == 0

        following = await reserve_turn(
            pool, principal, cid, later_user, write_event=write_event,
        )
        assert following["status"] == "queued"
        assert following["turn_id"] != reserved["turn_id"]

    async with pool.acquire() as connection:
        assert await connection.fetchval(
            "SELECT count(*) FROM conversation_turns "
            "WHERE tenant_id=$1 AND conversation_id=$2 AND user_message_id=$3",
            principal.tenant_id, cid, user,
        ) == 1
        assert await connection.fetchval(
            "SELECT count(*) FROM events WHERE correlation_id=$1 "
            "AND event_type='governance.conversation.turn.queued'",
            reserved["correlation_id"],
        ) == 1
        expected_event = (
            "governance.conversation.turn.completed"
            if recovery == "same_message_retry"
            else "governance.conversation.turn.cancelled"
        )
        assert await connection.fetchval(
            "SELECT count(*) FROM events WHERE correlation_id=$1 "
            "AND event_type=$2",
            reserved["correlation_id"], expected_event,
        ) == 1
