import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.conversation_coordinator as coordinator
from app.conversation_memory import (
    memory_context_digest,
    memory_system_message,
    stored_memory_context,
)


def snapshot(memories):
    return {
        "memory_context": json.dumps(memories, ensure_ascii=False),
        "memory_context_sha256": memory_context_digest(memories),
    }


@pytest.mark.parametrize("memories", [
    [], ["Spoken explanations first."], ["A", "B"], ["Café — project notes"],
])
def test_snapshot_round_trip(memories):
    stored = snapshot(memories)
    assert stored_memory_context(stored) == memories
    stored["memory_context"] = list(memories)
    assert stored_memory_context(stored) == memories
    assert memory_context_digest(tuple(memories)) == (
        memory_context_digest(memories)
    )


def test_digest_preserves_order_and_content():
    assert memory_context_digest(["A", "B"]) != memory_context_digest(["B", "A"])
    assert memory_context_digest(["A"]) != memory_context_digest(["A "])
    assert len(memory_context_digest([])) == 64


@pytest.mark.parametrize("value", [
    None, "not a list", {}, [""], [" "], [1], ["x" * 1001], ["x"] * 13,
])
def test_digest_rejects_invalid_context(value):
    with pytest.raises(ValueError):
        memory_context_digest(value)


@pytest.mark.parametrize("stored", [
    {},
    {"memory_context": None, "memory_context_sha256": None},
    {"memory_context": "not json", "memory_context_sha256": "0" * 64},
    {"memory_context": "{}", "memory_context_sha256": "0" * 64},
    {"memory_context": '["A"]', "memory_context_sha256": "0" * 64},
    {"memory_context": [], "memory_context_sha256": None},
])
def test_stored_snapshot_rejects_missing_invalid_or_changed_data(stored):
    with pytest.raises(ValueError):
        stored_memory_context(stored)


class Connection:
    def __init__(self, principal, conversation_id, message_id):
        self.principal = principal
        self.conversation_id = conversation_id
        self.message_id = message_id
        self.existing = None
        self.insert_args = None
        self.loaded_turn = None
        self.message_fetches = 0
        self.messages = [{
            "message_id": message_id,
            "sequence_number": 1,
            "role": "user",
            "content": "Continue the project.",
        }]
        self.completed_message = {
            "message_id": uuid4(),
            "conversation_id": conversation_id,
            "sequence_number": 2,
            "role": "assistant",
            "content": "Previously completed answer.",
            "client_message_id": None,
            "created_at": None,
        }

    @asynccontextmanager
    async def transaction(self):
        yield

    async def fetchrow(self, sql, *args):
        sql = " ".join(sql.split())
        if "SELECT conversation_id FROM conversations" in sql:
            assert args == (
                self.conversation_id,
                self.principal.tenant_id,
                self.principal.subject_id,
            )
            return {"conversation_id": self.conversation_id}
        if "SELECT role, sequence_number FROM conversation_messages" in sql:
            return {"role": "user", "sequence_number": 1}
        if "SELECT * FROM conversation_turns" in sql:
            return self.existing
        if "INSERT INTO conversation_turns" in sql:
            assert "memory_context, memory_context_sha256" in sql
            assert "$7::jsonb, $8" in sql
            self.insert_args = args
            return {
                "turn_id": args[0],
                "status": "queued",
                "correlation_id": args[0],
                "context_sequence_number": args[4],
                "context_sha256": args[5],
                "memory_context": args[6],
                "memory_context_sha256": args[7],
            }
        if "SELECT turn.*" in sql:
            return self.loaded_turn
        if "AND role = 'assistant'" in sql:
            return self.completed_message
        raise AssertionError(f"Unexpected query: {sql}")

    async def fetch(self, sql, *args):
        self.message_fetches += 1
        return self.messages

    async def fetchval(self, sql, *args):
        if "SELECT EXISTS" in sql:
            return False
        if "EXTRACT(EPOCH" in sql:
            return 30
        raise AssertionError(f"Unexpected scalar query: {sql}")


class Pool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.fixture
def rig():
    principal = SimpleNamespace(
        tenant_id=uuid4(),
        subject_id="snapshot-test-owner",
        permissions={"conversations:read", "conversations:write"},
    )
    cid, mid = uuid4(), uuid4()
    connection = Connection(principal, cid, mid)
    return SimpleNamespace(
        principal=principal,
        cid=cid,
        mid=mid,
        connection=connection,
        pool=Pool(connection),
        event=AsyncMock(),
    )


async def reserve(rig, memories):
    return await coordinator.reserve_turn(
        rig.pool, rig.principal, rig.cid, rig.mid,
        write_event=rig.event, memory_context=memories,
    )


@pytest.mark.asyncio
async def test_new_reservation_records_snapshot_and_metadata(rig):
    memories = ["Explain commands first.", "Project A constraint."]
    turn = await reserve(rig, memories)
    assert stored_memory_context(turn) == memories
    assert json.loads(rig.connection.insert_args[6]) == memories
    assert rig.connection.insert_args[7] == memory_context_digest(memories)
    payload = rig.event.await_args.kwargs["payload"]
    assert payload["memory_context_sha256"] == memory_context_digest(memories)
    assert payload["memory_count"] == 2
    assert "memory_context" not in payload


@pytest.mark.asyncio
async def test_queued_reservation_reuses_matching_snapshot(rig):
    turn = {"turn_id": uuid4(), "status": "queued", **snapshot(["A"])}
    rig.connection.existing = turn
    assert await reserve(rig, ["A"]) == turn
    assert rig.connection.insert_args is None
    rig.event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [
    {},
    snapshot(["B"]),
    {"memory_context": '["A"]', "memory_context_sha256": "0" * 64},
])
async def test_queued_reservation_rejects_missing_changed_or_corrupt_snapshot(
    rig, stored
):
    rig.connection.existing = {
        "turn_id": uuid4(), "status": "queued", **stored
    }
    with pytest.raises(
        coordinator.ConversationCoordinatorError, match="memory context"
    ):
        await reserve(rig, ["A"])
    assert rig.connection.insert_args is None
    rig.event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_historical_terminal_reservation_is_not_backfilled(rig, status):
    original = {"turn_id": uuid4(), "status": status}
    rig.connection.existing = original
    assert await reserve(rig, ["Current selection"]) == original
    assert "memory_context" not in original
    assert rig.connection.insert_args is None
    rig.event.assert_not_awaited()


def loadable_turn(rig, memories):
    return {
        "conversation_id": rig.cid,
        "user_message_id": rig.mid,
        "context_sequence_number": 1,
        "context_sha256": coordinator.context_digest(rig.connection.messages),
        **snapshot(memories),
    }


@pytest.mark.asyncio
async def test_loader_accepts_matching_snapshot(rig):
    rig.connection.loaded_turn = loadable_turn(rig, ["A"])
    rows, remaining = await coordinator._load_turn_context(
        rig.pool, rig.principal, uuid4(), uuid4(),
        expected_memory_sha256=memory_context_digest(["A"]),
    )
    assert rows == rig.connection.messages
    assert remaining == 30


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "corrupt", "replaced"])
async def test_loader_rejects_snapshot_changes_before_loading_messages(rig, mode):
    turn = loadable_turn(rig, ["A"])
    if mode == "legacy":
        turn.pop("memory_context")
        turn.pop("memory_context_sha256")
    elif mode == "corrupt":
        turn["memory_context"] = '["Changed"]'
    else:
        turn.update(snapshot(["Changed"]))
    rig.connection.loaded_turn = turn
    with pytest.raises(
        coordinator.ConversationCoordinatorError, match="memory context"
    ):
        await coordinator._load_turn_context(
            rig.pool, rig.principal, uuid4(), uuid4(),
            expected_memory_sha256=memory_context_digest(["A"]),
        )
    assert rig.connection.message_fetches == 0


@pytest.mark.asyncio
async def test_execution_uses_saved_selection_not_later_caller_mutation(
    rig, monkeypatch
):
    supplied = ["Original preference"]
    saved = {"turn_id": uuid4(), "status": "queued", **snapshot(supplied)}

    async def fake_reserve(*args, **kwargs):
        assert kwargs["memory_context"] == ["Original preference"]
        assert kwargs["memory_context"] is not supplied
        supplied[:] = ["Changed after reservation began"]
        return saved

    monkeypatch.setattr(coordinator, "reserve_turn", fake_reserve)
    claim = AsyncMock(return_value={
        "turn_id": saved["turn_id"], "worker_token": uuid4()
    })
    load = AsyncMock(return_value=(rig.connection.messages, 30))
    publish = AsyncMock(return_value={"result": "published"})
    fail = AsyncMock()
    monkeypatch.setattr(coordinator, "claim_turn", claim)
    monkeypatch.setattr(coordinator, "_load_turn_context", load)
    monkeypatch.setattr(coordinator, "publish_turn", publish)
    monkeypatch.setattr(coordinator, "fail_turn", fail)
    model = SimpleNamespace(complete=AsyncMock(return_value="Answer"))

    result = await coordinator.execute_turn(
        rig.pool, rig.principal, rig.cid, rig.mid,
        model=model, write_event=rig.event, memory_context=supplied,
    )
    assert result == {"result": "published"}
    context = model.complete.await_args.args[0]
    assert context[0].role == "system"
    assert context[0].content == memory_system_message(["Original preference"])
    assert load.await_args.kwargs["expected_memory_sha256"] == (
        saved["memory_context_sha256"]
    )
    publish.assert_awaited_once()
    fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_queued_execution_never_claims_or_calls_model(
    rig, monkeypatch
):
    monkeypatch.setattr(
        coordinator, "reserve_turn",
        AsyncMock(return_value={"turn_id": uuid4(), "status": "queued"}),
    )
    claim = AsyncMock()
    monkeypatch.setattr(coordinator, "claim_turn", claim)
    model = SimpleNamespace(complete=AsyncMock())

    with pytest.raises(
        coordinator.ConversationCoordinatorError, match="memory context"
    ):
        await coordinator.execute_turn(
            rig.pool, rig.principal, rig.cid, rig.mid,
            model=model, write_event=rig.event, memory_context=["New selection"],
        )
    claim.assert_not_awaited()
    model.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_completed_execution_returns_saved_result_without_snapshot(
    rig, monkeypatch
):
    completed = {
        "turn_id": uuid4(),
        "status": "completed",
        "assistant_message_id": rig.connection.completed_message["message_id"],
    }
    monkeypatch.setattr(
        coordinator, "reserve_turn", AsyncMock(return_value=completed)
    )
    claim = AsyncMock()
    monkeypatch.setattr(coordinator, "claim_turn", claim)
    model = SimpleNamespace(complete=AsyncMock())

    result = await coordinator.execute_turn(
        rig.pool, rig.principal, rig.cid, rig.mid,
        model=model, write_event=rig.event, memory_context=["Current selection"],
    )
    assert result["turn"] == completed
    assert result["message"] == rig.connection.completed_message
    claim.assert_not_awaited()
    model.complete.assert_not_awaited()
