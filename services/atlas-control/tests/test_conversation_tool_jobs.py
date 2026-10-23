import hashlib
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from uuid import uuid4
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth import Principal
from app import main
from app import conversation_coordinator as co
from app.conversation_coordinator import ConversationCoordinatorError
from app.conversation_tool_calls import ModelToolCall, StructuredModelReply
from app import conversation_tool_jobs as bridge

def actor(permissions=None):
    if permissions is None:
        permissions = {"conversations:read", "conversations:write", "jobs:create"}
    return Principal("test-owner", uuid4(), frozenset(permissions), "user")

def turn():
    return {'turn_id': uuid4(), 'conversation_id': uuid4(), 'correlation_id': uuid4(), 'user_message_id': uuid4()}

def call(raw='{"content":"qualification"}', name="atlas_metadata_analyze"):
    return ModelToolCall("call-1", name, raw)

def database():
    connection = AsyncMock()
    transaction = MagicMock()
    transaction.__aenter__.return_value = connection
    transaction.__aexit__.return_value = False
    connection.transaction = Mock(return_value=transaction)
    acquire = MagicMock()
    acquire.__aenter__.return_value = connection
    acquire.__aexit__.return_value = False
    pool = Mock()
    pool.acquire.return_value = acquire
    return pool, connection, transaction

@pytest.mark.asyncio
async def test_exact_proposal_binding_and_required_approval():
    connection, principal, source = Mock(), actor(), turn()
    proposal = call('{ "content": "qualification" }')
    with patch.object(
        main, "_create_job_on_connection", new_callable=AsyncMock,
        return_value={"job_id": "job-1"},
    ) as create:
        await bridge.prepare_tool_job(connection, principal, source, proposal)
    args = create.await_args.args
    assert args[0] is connection and args[2] is principal
    job = args[1]
    assert job.approval_required is True
    assert job.job_type == "metadata.analyze"
    assert job.correlation_id == source["correlation_id"]
    assert job.request_payload["content"] == "qualification"
    origin = job.request_payload["_atlas_proposal"]
    assert origin["turn_id"] == str(source["turn_id"])
    assert origin["conversation_id"] == str(source["conversation_id"])
    assert origin["subject_id"] == principal.subject_id
    assert origin["tool_call_id"] == proposal.id
    assert origin["arguments_json"] == proposal.arguments_json
    assert origin["arguments_sha256"] == hashlib.sha256(
        proposal.arguments_json.encode()
    ).hexdigest()

@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [
    "not-json", "[]", "{}", '{"content":""}', '{"content":"   "}',
    '{"content":123}', '{"content":"ok","approval_required":false}',
    '{"content":"one","content":"two"}', '{"content":NaN}',
])
async def test_invalid_arguments_make_no_job(raw):
    with patch.object(main, "_create_job_on_connection", new_callable=AsyncMock) as create:
        with pytest.raises(ConversationCoordinatorError, match="invalid tool proposal"):
            await bridge.prepare_tool_job(Mock(), actor(), turn(), call(raw))
        create.assert_not_awaited()

@pytest.mark.asyncio
async def test_unknown_tool_and_missing_permission_make_no_job():
    with patch.object(main, "_create_job_on_connection", new_callable=AsyncMock) as create:
        with pytest.raises(ConversationCoordinatorError, match="invalid tool proposal"):
            await bridge.prepare_tool_job(Mock(), actor(), turn(), call(name="other"))
        with pytest.raises(ConversationCoordinatorError, match="permission denied"):
            await bridge.prepare_tool_job(Mock(), actor([]), turn(), call())
        create.assert_not_awaited()

@pytest.mark.asyncio
async def test_structured_execution_reaches_publication():
    source, principal, token = turn(), actor(), uuid4()
    reserved = dict(source, status="queued", memory_context_sha256="digest")
    model = Mock()
    model.complete = AsyncMock()
    proposal = call()
    model.complete_with_tools = AsyncMock(
        return_value=StructuredModelReply("", (proposal,), "tool_calls")
    )
    with (
        patch.object(co, "reserve_turn", new_callable=AsyncMock, return_value=reserved),
        patch.object(co, "claim_turn", new_callable=AsyncMock,
                     return_value=dict(source, worker_token=token)),
        patch.object(co, "stored_memory_context", return_value=[]),
        patch.object(co, "_load_turn_context", new_callable=AsyncMock,
                     return_value=([{"role": "user", "content": "analyze"}], 10)),
        patch.object(co, "publish_turn", new_callable=AsyncMock,
                     return_value={"ok": True}) as publish,
    ):
        result = await co.execute_turn(
            Mock(), principal, source["conversation_id"], uuid4(),
            model=model, write_event=AsyncMock(), tool_mode=True,
        )
    assert result == {"ok": True}
    model.complete.assert_not_awaited()
    assert model.complete_with_tools.await_args.kwargs["tools"] == bridge.proposal_tools()
    assert publish.await_args.kwargs["tool_calls"] == (proposal,)

@pytest.mark.asyncio
async def test_unavailable_turn_cannot_create_proposal_job():
    pool, connection, transaction = database()
    connection.fetchrow.return_value = None
    with (
        patch.object(co, "execution_lock", new_callable=AsyncMock),
        patch.object(bridge, "prepare_tool_job", new_callable=AsyncMock) as prepare,
    ):
        with pytest.raises(ConversationCoordinatorError, match="turn unavailable"):
            await co.publish_turn(
                pool, actor(), uuid4(), worker_token=uuid4(),
                content="", write_event=AsyncMock(), tool_calls=(call(),),
            )
    prepare.assert_not_awaited()
    assert transaction.__aexit__.await_args.args[0] is ConversationCoordinatorError

@pytest.mark.asyncio
async def test_proposal_uses_publication_connection_and_acknowledgement():
    pool, connection, transaction = database()
    source = turn()
    message = {"message_id": uuid4(), "content": "acknowledgement"}
    connection.fetchrow.side_effect = [
        source, {"conversation_id": source["conversation_id"]},
        message, {"attempt": 1},
    ]
    connection.fetchval.return_value = "user"
    principal, proposal = actor(), call()
    with (
        patch.object(co, "execution_lock", new_callable=AsyncMock),
        patch.object(co, "_memory_lifecycle_gate", new_callable=AsyncMock) as gate,
        patch.object(bridge, "prepare_tool_job", new_callable=AsyncMock,
                     return_value={"job_id": "job-1", "approval_gate_id": "gate-1"}) as prepare,
    ):
        result = await co.publish_turn(
            pool, principal, source["turn_id"], worker_token=uuid4(),
            content="", write_event=AsyncMock(), tool_calls=(proposal,),
        )
    gate.assert_awaited_once()
    prepare.assert_awaited_once_with(connection, principal, source, proposal)
    inserted_content = connection.fetchrow.await_args_list[2].args[4]
    assert "job-1" in inserted_content and "gate-1" in inserted_content
    assert "has not been executed" in inserted_content
    assert result["message"] == message
    assert transaction.__aexit__.await_args.args[0] is None

@pytest.mark.asyncio
async def test_multiple_proposals_rejected_before_database():
    pool = Mock()
    with pytest.raises(ConversationCoordinatorError, match="one tool proposal"):
        await co.publish_turn(
            pool, actor(), uuid4(), worker_token=uuid4(), content="",
            write_event=AsyncMock(), tool_calls=(call(), call()),
        )
    pool.acquire.assert_not_called()
