from uuid import UUID

import httpx
import pytest
import pytest_asyncio

from app.conversation_memory import stored_memory_context
from app.conversation_memory_lifecycle import transition_memory
from app.main import app, write_event
from test_workspace_connected_postgres import workspace_case
from test_accepted_memory_enforcement import new_source, proposal, reserve


@pytest_asyncio.fixture
async def lifecycle_case(workspace_case, monkeypatch):
    monkeypatch.setenv("ATLAS_ACCEPTED_MEMORY_RECALL", "1")
    monkeypatch.setenv("ATLAS_MEMORY_LIFECYCLE_ENFORCEMENT", "1")
    case = workspace_case
    assert await case["connection"].fetchval(
        "SELECT to_regclass('conversation_memory_transitions')"
    ) is not None, "Migration 019 is missing from the isolated database"
    return case


async def setup_pair(case):
    source = await new_source(case)
    old = await proposal(case, source, "Original memory", scope="personal")
    new = await proposal(case, source, "Corrected memory", status="proposed")
    return old, new


async def get_proposal(case, pid):
    return await case["connection"].fetchrow(
        "SELECT * FROM conversation_memory_proposals WHERE proposal_id=$1", pid,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revoke", "supersede"])
async def test_http_transition_history_replay_and_recall(lifecycle_case, action):
    case = lifecycle_case
    old, new = await setup_pair(case)
    before = await get_proposal(case, old)
    body = {"reason": "User requested memory correction"}
    if action == "supersede":
        body["replacement_proposal_id"] = str(new)
    path = f"/memory-proposals/{old}/{action}"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        for headers, expected in (
            ({}, 401),
            ({"Authorization": "Bearer dev-worker"}, 403),
            ({"Authorization": "Bearer dev-admin"}, 404),
        ):
            response = await client.post(path, headers=headers, json=body)
            assert response.status_code == expected, response.text

        headers = {"Authorization": "Bearer dev-requester"}
        response = await client.post(path, headers=headers, json=body)
        assert response.status_code == 200, response.text
        repeat = await client.post(path, headers=headers, json=body)
        assert repeat.status_code == 200, repeat.text
        assert repeat.json() == response.json()

        conflict = await client.post(
            path, headers=headers, json={**body, "reason": "Different request"},
        )
        assert conflict.status_code == 409, conflict.text

    after = await get_proposal(case, old)
    for field in (
        "content", "status", "reviewed_at", "reviewed_by_subject_id",
        "memory_scope", "workspace_project_id",
    ):
        assert after[field] == before[field]
    expected_state = "revoked" if action == "revoke" else "superseded"
    assert after["lifecycle_state"] == expected_state
    assert after["superseded_by_proposal_id"] == (
        new if action == "supersede" else None
    )

    replacement = await get_proposal(case, new)
    assert replacement["status"] == (
        "accepted" if action == "supersede" else "proposed"
    )
    if action == "supersede":
        assert replacement["memory_scope"] == "personal"
        assert replacement["reviewed_by_subject_id"] == case["principal"].subject_id

    transition_id = UUID(response.json()["transition_id"])
    connection = case["connection"]
    assert await connection.fetchval(
        "SELECT count(*) FROM conversation_memory_transitions "
        "WHERE tenant_id=$1 AND proposal_id=$2",
        case["principal"].tenant_id, old,
    ) == 1
    assert await connection.fetchval(
        "SELECT count(*) FROM events WHERE correlation_id=$1", transition_id,
    ) == 1

    turn = await reserve(case)
    assert stored_memory_context(turn) == (
        ["Corrected memory"] if action == "supersede" else []
    )
    assert turn["memory_dependencies"] is not None
    assert turn["memory_dependencies_sha256"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revoke", "supersede"])
async def test_transition_event_failure_rolls_back_every_change(lifecycle_case, action):
    case = lifecycle_case
    old, new = await setup_pair(case)
    old_before = dict(await get_proposal(case, old))
    new_before = dict(await get_proposal(case, new))
    correlations = []

    async def failing_event(connection, **fields):
        await write_event(connection, **fields)
        correlations.append(fields["correlation_id"])
        raise RuntimeError("injected lifecycle audit failure")

    with pytest.raises(RuntimeError, match="injected lifecycle audit"):
        await transition_memory(
            case["pool"], case["principal"], old,
            reason="Rollback verification", write_event=failing_event,
            replacement_proposal_id=new if action == "supersede" else None,
        )

    assert dict(await get_proposal(case, old)) == old_before
    assert dict(await get_proposal(case, new)) == new_before
    assert await case["connection"].fetchval(
        "SELECT count(*) FROM conversation_memory_transitions "
        "WHERE tenant_id=$1 AND proposal_id=$2",
        case["principal"].tenant_id, old,
    ) == 0
    assert len(correlations) == 1
    assert await case["connection"].fetchval(
        "SELECT count(*) FROM events WHERE correlation_id=$1", correlations[0],
    ) == 0


from unittest.mock import AsyncMock
from uuid import uuid4

from app.conversation_coordinator import (
    ConversationCoordinatorError, claim_turn, execute_turn,
    publish_turn, _load_turn_context,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revoke", "supersede"])
@pytest.mark.parametrize("stage", ["queued", "running"])
async def test_withdrawal_blocks_unfinished_turn(lifecycle_case, action, stage):
    case = lifecycle_case
    old, new = await setup_pair(case)
    turn = await reserve(case)
    assert stored_memory_context(turn) == ["Original memory"]
    running = None

    if stage == "running":
        running = await claim_turn(
            case["pool"], case["principal"], turn["turn_id"],
            write_event=write_event,
        )
        rows, remaining = await _load_turn_context(
            case["pool"], case["principal"], turn["turn_id"],
            running["worker_token"],
            expected_memory_sha256=turn["memory_context_sha256"],
        )
        assert rows and remaining > 0

    await transition_memory(
        case["pool"], case["principal"], old,
        reason="Withdraw memory used by unfinished turn",
        write_event=write_event,
        replacement_proposal_id=new if action == "supersede" else None,
    )

    if stage == "queued":
        with pytest.raises(ConversationCoordinatorError, match="memory"):
            await claim_turn(
                case["pool"], case["principal"], turn["turn_id"],
                write_event=write_event,
            )
        model = AsyncMock()
        with pytest.raises(ConversationCoordinatorError, match="memory"):
            await execute_turn(
                case["pool"], case["principal"], case["cid"], case["user"],
                model=model, write_event=write_event,
            )
        model.complete.assert_not_awaited()
    else:
        with pytest.raises(ConversationCoordinatorError, match="memory"):
            await _load_turn_context(
                case["pool"], case["principal"], turn["turn_id"],
                running["worker_token"],
                expected_memory_sha256=turn["memory_context_sha256"],
            )
        with pytest.raises(ConversationCoordinatorError, match="memory"):
            await publish_turn(
                case["pool"], case["principal"], turn["turn_id"],
                worker_token=running["worker_token"],
                content="This obsolete response must not be published",
                write_event=write_event,
            )

    connection = case["connection"]
    stored = await connection.fetchrow(
        "SELECT * FROM conversation_turns WHERE turn_id=$1", turn["turn_id"],
    )
    assert stored["status"] == stage
    assert stored["attempt"] == (0 if stage == "queued" else 1)
    assert stored_memory_context(dict(stored)) == ["Original memory"]
    assert await connection.fetchval(
        "SELECT count(*) FROM conversation_messages "
        "WHERE conversation_id=$1 AND role='assistant'", case["cid"],
    ) == 0
    assert await connection.fetchval(
        "SELECT count(*) FROM events WHERE correlation_id=$1 "
        "AND event_type='governance.conversation.turn.completed'",
        turn["turn_id"],
    ) == 0
    assert await connection.fetchval(
        "SELECT count(*) FROM events WHERE correlation_id=$1 "
        "AND event_type='governance.conversation.turn.running'",
        turn["turn_id"],
    ) == (0 if stage == "queued" else 1)


@pytest.mark.asyncio
async def test_nonempty_legacy_snapshot_cannot_be_claimed(lifecycle_case):
    case = lifecycle_case
    await setup_pair(case)
    turn = await reserve(case)
    await case["connection"].execute(
        "UPDATE conversation_turns SET memory_dependencies=NULL,"
        "memory_dependencies_sha256=NULL WHERE turn_id=$1",
        turn["turn_id"],
    )
    with pytest.raises(ConversationCoordinatorError, match="memory"):
        await claim_turn(
            case["pool"], case["principal"], turn["turn_id"],
            write_event=write_event,
        )
    assert await case["connection"].fetchval(
        "SELECT status FROM conversation_turns WHERE turn_id=$1",
        turn["turn_id"],
    ) == "queued"


@pytest.mark.asyncio
async def test_disabling_enforcement_cannot_claim_linked_turn(
    lifecycle_case, monkeypatch,
):
    case = lifecycle_case
    await setup_pair(case)
    turn = await reserve(case)
    monkeypatch.setenv("ATLAS_MEMORY_LIFECYCLE_ENFORCEMENT", "0")
    with pytest.raises(ConversationCoordinatorError, match="memory"):
        await claim_turn(
            case["pool"], case["principal"], turn["turn_id"],
            write_event=write_event,
        )
    assert await case["connection"].fetchval(
        "SELECT status FROM conversation_turns WHERE turn_id=$1",
        turn["turn_id"],
    ) == "queued"


@pytest.mark.asyncio
async def test_untracked_saved_context_is_rejected_atomically(lifecycle_case):
    case = lifecycle_case
    with pytest.raises(ConversationCoordinatorError, match="untracked"):
        await reserve(
            case, memory_context=["Untracked saved fact"],
            workspace_project_id=uuid4(),
        )
    for table in ("conversation_turns", "conversation_workspace_bindings"):
        assert await case["connection"].fetchval(
            "SELECT count(*) FROM " + table + " WHERE conversation_id=$1",
            case["cid"],
        ) == 0
    assert await case["connection"].fetchval(
        "SELECT count(*) FROM events WHERE correlation_id=$1 "
        "AND event_type='governance.conversation.workspace.bound'",
        case["cid"],
    ) == 0
