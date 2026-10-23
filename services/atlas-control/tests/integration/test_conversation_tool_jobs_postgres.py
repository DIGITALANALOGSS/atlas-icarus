import json
import os
from unittest.mock import Mock
from urllib.parse import urlparse
from uuid import uuid4
import pytest

from app.auth import Principal, development_principals
from app.conversation_coordinator import (
    ConversationCoordinatorError, reserve_turn, claim_turn,
    cancel_turn, publish_turn, execute_turn,
)
from app.conversation_tool_calls import ModelToolCall
from app.main import app, write_event
from test_conversation_lifecycle import BoundPool

def decoded(value):
    return json.loads(value) if isinstance(value, str) else value

@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    "success_and_replay", "cancel", "expiry", "wrong_token", "audit_rollback",
])
async def test_bridge_postgres_boundary(monkeypatch, scenario):
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
    arguments = '{ "content": "database bridge qualification" }'
    proposal = ModelToolCall("db-call-1", "atlas_metadata_analyze", arguments)

    async with app.router.lifespan_context(app):
        async with app.state.pool.acquire() as connection:
            outer = connection.transaction()
            await outer.start()
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
                    "VALUES ($1,$2,$3,1,'user','Analyze this text')",
                    user, principal.tenant_id, cid,
                )
                pool = BoundPool(connection)
                reserved = await reserve_turn(
                    pool, principal, cid, user, write_event=write_event,
                )
                running = await claim_turn(
                    pool, principal, reserved["turn_id"], write_event=write_event,
                )
                tid, token = running["turn_id"], running["worker_token"]
                correlation = running["correlation_id"]
                if scenario == "cancel":
                    await cancel_turn(pool, principal, tid, write_event=write_event)
                elif scenario == "expiry":
                    await connection.execute(
                        "UPDATE conversation_turns SET lease_expires_at="
                        "clock_timestamp()-INTERVAL '1 second' WHERE turn_id=$1", tid,
                    )
                elif scenario == "wrong_token":
                    token = uuid4()

                async def audit(conn, **kwargs):
                    await write_event(conn, **kwargs)
                    if scenario == "audit_rollback":
                        raise RuntimeError("injected bridge audit failure")

                async def publish():
                    return await publish_turn(
                        pool, principal, tid, worker_token=token, content="",
                        write_event=audit, tool_calls=(proposal,),
                    )

                success = scenario == "success_and_replay"
                if success:
                    result = await publish()
                    model = Mock()
                    replay = await execute_turn(
                        pool, principal, cid, user, model=model,
                        write_event=write_event, tool_mode=True,
                    )
                    assert replay == result
                    model.complete.assert_not_called()
                    model.complete_with_tools.assert_not_called()
                    with pytest.raises(ConversationCoordinatorError):
                        await publish()
                elif scenario == "audit_rollback":
                    with pytest.raises(RuntimeError, match="injected bridge"):
                        await publish()
                else:
                    with pytest.raises(ConversationCoordinatorError):
                        await publish()

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
                assert len(jobs) == len(gates) == assistants == int(success)
                if success:
                    job, gate = jobs[0], gates[0]
                    assert job["status"] == "pending_approval"
                    assert job["approval_required"] is True
                    assert job["approval_gate_id"] == gate["gate_id"]
                    assert gate["status"] == "pending"
                    payload = decoded(job["request_payload"])
                    origin = payload["_atlas_proposal"]
                    assert payload["content"] == "database bridge qualification"
                    assert origin["turn_id"] == str(tid)
                    assert origin["conversation_id"] == str(cid)
                    assert origin["tool_call_id"] == proposal.id
                    assert origin["arguments_json"] == arguments
                    action = decoded(gate["action_payload"])
                    assert action["request_payload"] == payload
                    assert action["job_id"] == str(job["job_id"])
                elif scenario == "audit_rollback":
                    assert await connection.fetchval(
                        "SELECT status FROM conversation_turns WHERE turn_id=$1", tid,
                    ) == "running"
                    assert await connection.fetchval(
                        "SELECT count(*) FROM events WHERE correlation_id=$1 "
                        "AND event_type IN ('jobs.created','jobs.approval_requested',"
                        "'governance.approval_gate.created',"
                        "'governance.conversation.turn.completed')",
                        correlation,
                    ) == 0
            finally:
                await outer.rollback()
