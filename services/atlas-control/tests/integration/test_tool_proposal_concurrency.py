import asyncio
import os
from urllib.parse import urlparse
from uuid import uuid4
import pytest

from app.auth import Principal, development_principals
from app.conversation_coordinator import (
    ConversationCoordinatorError, reserve_turn, claim_turn, publish_turn,
)
from app.conversation_tool_calls import ModelToolCall
from app.main import app, write_event
from test_conversation_lifecycle import BoundPool

@pytest.mark.asyncio
async def test_competing_publishers_create_exactly_one_proposal(monkeypatch):
    database = urlparse(os.environ.get("DATABASE_URL", ""))
    if database.hostname != "postgres-test" or database.path != "/atlas_test":
        pytest.fail("Requires isolated postgres-test/atlas_test")
    monkeypatch.setenv("ATLAS_CONVERSATION_MODEL", "echo")
    base = development_principals()["dev-requester"]
    principal = Principal(
        base.subject_id, base.tenant_id,
        base.permissions | frozenset({"jobs:create"}), base.principal_type,
    )
    cid, user = uuid4(), uuid4()
    correlation = None
    proposal = ModelToolCall(
        "concurrent-call-1", "atlas_metadata_analyze",
        '{"content":"concurrent publication qualification"}',
    )

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
                        "VALUES ($1,$2,$3,1,'user','Analyze this text')",
                        user, principal.tenant_id, cid,
                    )
            reserved = await reserve_turn(
                pool, principal, cid, user, write_event=write_event,
            )
            correlation = reserved["correlation_id"]
            running = await claim_turn(
                pool, principal, reserved["turn_id"], write_event=write_event,
            )
            tid, token = running["turn_id"], running["worker_token"]
            paused, release = asyncio.Event(), asyncio.Event()
            tasks = []

            async def pausing_audit(connection, **kwargs):
                await write_event(connection, **kwargs)
                if kwargs["event_type"] == "governance.conversation.turn.completed":
                    paused.set()
                    await release.wait()

            async with pool.acquire() as first:
                async with pool.acquire() as second:
                    first_pid = await first.fetchval("SELECT pg_backend_pid()")
                    second_pid = await second.fetchval("SELECT pg_backend_pid()")
                    assert first_pid != second_pid

                    async def publish(connection, audit):
                        return await publish_turn(
                            BoundPool(connection), principal, tid,
                            worker_token=token, content="",
                            write_event=audit, tool_calls=(proposal,),
                        )

                    try:
                        tasks.append(asyncio.create_task(publish(first, pausing_audit)))
                        await asyncio.wait_for(paused.wait(), 10)
                        tasks.append(asyncio.create_task(publish(second, write_event)))
                        blocked = False
                        for _ in range(100):
                            blocked = await first.fetchval(
                                "SELECT $1::integer = ANY(pg_blocking_pids($2::integer))",
                                first_pid, second_pid,
                            )
                            if blocked or tasks[1].done():
                                break
                            await asyncio.sleep(.05)
                        assert blocked, "Second publisher did not wait on first"
                        release.set()
                        results = await asyncio.wait_for(
                            asyncio.gather(*tasks, return_exceptions=True), 15,
                        )
                        assert isinstance(results[0], dict)
                        assert isinstance(results[1], ConversationCoordinatorError)
                    finally:
                        release.set()
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)

            async with pool.acquire() as connection:
                jobs = await connection.fetch(
                    "SELECT * FROM jobs WHERE tenant_id=$1 AND correlation_id=$2",
                    principal.tenant_id, correlation,
                )
                gates = await connection.fetch(
                    "SELECT * FROM approval_gates WHERE tenant_id=$1 AND correlation_id=$2",
                    principal.tenant_id, correlation,
                )
                assistants = await connection.fetchval(
                    "SELECT count(*) FROM conversation_messages "
                    "WHERE tenant_id=$1 AND conversation_id=$2 AND role='assistant'",
                    principal.tenant_id, cid,
                )
                assert len(jobs) == len(gates) == assistants == 1
                assert jobs[0]["status"] == "pending_approval"
                assert gates[0]["status"] == "pending"
                assert jobs[0]["approval_gate_id"] == gates[0]["gate_id"]
                assert await connection.fetchval(
                    "SELECT count(*) FROM events WHERE correlation_id=$1 "
                    "AND event_type='governance.conversation.turn.completed'",
                    correlation,
                ) == 1
        finally:
            async with pool.acquire() as connection:
                async with connection.transaction():
                    if correlation is not None:
                        for table in ("jobs", "approval_gates", "events"):
                            await connection.execute(
                                f"DELETE FROM {table} WHERE tenant_id=$1 AND correlation_id=$2",
                                principal.tenant_id, correlation,
                            )
                    for table in (
                        "conversation_learning_jobs",
                        "conversation_turns",
                        "conversation_messages",
                    ):
                        await connection.execute(
                            f"DELETE FROM {table} WHERE tenant_id=$1 AND conversation_id=$2",
                            principal.tenant_id, cid,
                        )
                    await connection.execute(
                        "DELETE FROM conversations WHERE tenant_id=$1 AND conversation_id=$2",
                        principal.tenant_id, cid,
                    )
