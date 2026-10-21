import asyncio

import pytest
import pytest_asyncio

from app.conversation_coordinator import (
    ConversationCoordinatorError, claim_turn, publish_turn,
)
from app.conversation_memory_lifecycle import transition_memory
from app.main import write_event
from test_conversation_concurrency import conversation_case
from test_workspace_connected_postgres import BoundPool
from test_accepted_memory_enforcement import proposal, reserve


@pytest_asyncio.fixture
async def race_case(conversation_case, monkeypatch):
    monkeypatch.setenv("ATLAS_ACCEPTED_MEMORY_RECALL", "1")
    monkeypatch.setenv("ATLAS_MEMORY_LIFECYCLE_ENFORCEMENT", "1")
    pool, principal, cid, user = conversation_case
    async with pool.acquire() as connection:
        case = {
            "pool": pool, "connection": connection,
            "principal": principal, "cid": cid, "user": user,
        }
        try:
            yield case
        finally:
            async with connection.transaction():
                correlations = [cid]
                correlations += [
                    r["turn_id"] for r in await connection.fetch(
                        "SELECT turn_id FROM conversation_turns "
                        "WHERE tenant_id=$1 AND conversation_id=$2",
                        principal.tenant_id, cid,
                    )
                ]
                correlations += [
                    r["transition_id"] for r in await connection.fetch(
                        "SELECT t.transition_id "
                        "FROM conversation_memory_transitions AS t "
                        "JOIN conversation_memory_proposals AS p "
                        "ON p.tenant_id=t.tenant_id AND p.proposal_id=t.proposal_id "
                        "WHERE p.tenant_id=$1 AND p.conversation_id=$2",
                        principal.tenant_id, cid,
                    )
                ]
                await connection.execute(
                    "DELETE FROM events WHERE tenant_id=$1 "
                    "AND correlation_id=ANY($2::uuid[])",
                    principal.tenant_id, correlations,
                )
                await connection.execute(
                    "DELETE FROM conversation_memory_transitions AS t "
                    "USING conversation_memory_proposals AS p "
                    "WHERE p.tenant_id=t.tenant_id AND p.proposal_id=t.proposal_id "
                    "AND p.tenant_id=$1 AND p.conversation_id=$2",
                    principal.tenant_id, cid,
                )


async def confirm_blocked(observer, blocked_pid, blocker_pid, task):
    deadline = asyncio.get_running_loop().time() + 4
    while asyncio.get_running_loop().time() < deadline:
        assert not task.done(), "Competing operation did not wait"
        blockers = await observer.fetchval(
            "SELECT pg_blocking_pids($1::int)", blocked_pid,
        )
        if blocker_pid in blockers:
            return
        await asyncio.sleep(0.02)
    pytest.fail("Database lock wait was not observed")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revoke", "supersede"])
@pytest.mark.parametrize("first_operation", ["withdrawal", "publication"])
async def test_committed_lifecycle_race(race_case, action, first_operation):
    case = race_case
    src = case["principal"].tenant_id, case["cid"], case["user"]
    old = await proposal(case, src, "Original race memory", scope="personal")
    new = await proposal(case, src, "Replacement race memory", status="proposed")
    turn = await reserve(case)
    running = await claim_turn(
        case["pool"], case["principal"], turn["turn_id"], write_event=write_event,
    )

    async def withdraw(connection):
        return await transition_memory(
            BoundPool(connection), case["principal"], old,
            reason="Concurrent lifecycle verification",
            write_event=write_event,
            replacement_proposal_id=new if action == "supersede" else None,
        )

    async def publish(connection):
        return await publish_turn(
            BoundPool(connection), case["principal"], turn["turn_id"],
            worker_token=running["worker_token"],
            content="Response authorized before withdrawal",
            write_event=write_event,
        )

    async with case["pool"].acquire(timeout=5) as first:
        async with case["pool"].acquire(timeout=5) as second:
            first_pid = await first.fetchval("SELECT pg_backend_pid()")
            second_pid = await second.fetchval("SELECT pg_backend_pid()")
            assert first_pid != second_pid
            transaction = first.transaction()
            await transaction.start()
            committed, task = False, None
            try:
                if first_operation == "withdrawal":
                    await withdraw(first)
                    task = asyncio.create_task(
                        asyncio.wait_for(publish(second), 10)
                    )
                else:
                    await publish(first)
                    task = asyncio.create_task(
                        asyncio.wait_for(withdraw(second), 10)
                    )
                await confirm_blocked(
                    case["connection"], second_pid, first_pid, task,
                )
                await transaction.commit()
                committed = True
                if first_operation == "withdrawal":
                    with pytest.raises(ConversationCoordinatorError, match="memory"):
                        await task
                else:
                    await task
            finally:
                if not committed and first.is_in_transaction():
                    await transaction.rollback()
                if task is not None:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    connection = case["connection"]
    assert await connection.fetchval(
        "SELECT lifecycle_state FROM conversation_memory_proposals "
        "WHERE proposal_id=$1", old,
    ) == ("revoked" if action == "revoke" else "superseded")
    assert await connection.fetchval(
        "SELECT status FROM conversation_turns WHERE turn_id=$1", turn["turn_id"],
    ) == ("running" if first_operation == "withdrawal" else "completed")
    expected = 0 if first_operation == "withdrawal" else 1
    assert await connection.fetchval(
        "SELECT count(*) FROM conversation_messages "
        "WHERE conversation_id=$1 AND role='assistant'", case["cid"],
    ) == expected
    assert await connection.fetchval(
        "SELECT count(*) FROM events WHERE correlation_id=$1 "
        "AND event_type='governance.conversation.turn.completed'", turn["turn_id"],
    ) == expected
