import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth import Principal
from app.conversation_coordinator import ConversationCoordinatorError
from app.conversation_tool_calls import ModelToolCall
from app.conversation_tool_jobs import proposal_tools
from app.conversation_tool_read import read_tool_job


def principal(permissions=None):
    if permissions is None:
        permissions = {
            "jobs:read", "conversations:read", "conversations:write",
        }
    return Principal("owner", uuid4(), frozenset(permissions), "user")


def call(arguments=None):
    if arguments is None:
        arguments = json.dumps({"job_id": str(uuid4())})
    return ModelToolCall("read-1", "atlas_job_status", arguments)


def row():
    return {
        "job_id": uuid4(), "job_type": "metadata.analyze",
        "status": "succeeded", "result_payload": {"words": 4},
        "error_code": None, "approval_gate_id": uuid4(),
        "approval_status": "approved",
        "started_at": None, "completed_at": None,
    }


def test_registry_preserves_proposal_and_adds_lookup():
    assert [
        tool["function"]["name"] for tool in proposal_tools()
    ] == ["atlas_metadata_analyze", "atlas_job_status"]


@pytest.mark.asyncio
async def test_returns_stored_result_without_writes():
    connection = AsyncMock()
    connection.fetchrow.return_value = row()
    actor, conversation = principal(), uuid4()
    reply = await read_tool_job(
        connection, actor, {"conversation_id": conversation}, call(),
    )
    assert '"status": "succeeded"' in reply
    assert '"words": 4' in reply
    assert connection.fetchrow.await_args.args[2] == actor.tenant_id
    assert connection.fetchrow.await_args.args[3] == conversation
    assert connection.fetchrow.await_args.args[4] == actor.subject_id
    connection.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_permission_rejected_before_query():
    connection = AsyncMock()
    with pytest.raises(ConversationCoordinatorError, match="permission denied"):
        await read_tool_job(
            connection, principal(set()), {"conversation_id": uuid4()}, call(),
        )
    connection.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    "{}", '{"job_id": 1}', '{"job_id": "bad"}',
    '{"job_id": null}', '{"job_id": "bad", "extra": true}',
    '{"job_id": "a", "job_id": "b"}',
])
async def test_invalid_arguments_rejected_before_query(raw):
    connection = AsyncMock()
    with pytest.raises(ConversationCoordinatorError, match="invalid job lookup"):
        await read_tool_job(
            connection, principal(), {"conversation_id": uuid4()}, call(raw),
        )
    connection.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_job_uses_generic_error():
    connection = AsyncMock()
    connection.fetchrow.return_value = None
    with pytest.raises(ConversationCoordinatorError, match="job unavailable"):
        await read_tool_job(
            connection, principal(), {"conversation_id": uuid4()}, call(),
        )


@pytest.mark.asyncio
async def test_large_result_is_explicitly_truncated():
    connection = AsyncMock()
    value = row()
    value["result_payload"] = {"text": "x" * 10000}
    connection.fetchrow.return_value = value
    reply = await read_tool_job(
        connection, principal(), {"conversation_id": uuid4()}, call(),
    )
    assert '"result_truncated": true' in reply
    assert len(reply) < 8000
