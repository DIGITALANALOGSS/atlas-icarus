import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.conversation_coordinator as coordinator
from app.conversation_memory import memory_context_digest, stored_memory_context
from app.conversation_memory_recall import (
    accepted_memory_recall_enabled,
    merge_memory_context,
    select_accepted_memories,
)
from test_turn_memory_snapshots import rig, snapshot


def test_activation_is_explicit(monkeypatch):
    monkeypatch.delenv("ATLAS_ACCEPTED_MEMORY_RECALL", raising=False)
    assert not accepted_memory_recall_enabled()
    monkeypatch.setenv("ATLAS_ACCEPTED_MEMORY_RECALL", "true")
    assert not accepted_memory_recall_enabled()
    monkeypatch.setenv("ATLAS_ACCEPTED_MEMORY_RECALL", "1")
    assert accepted_memory_recall_enabled()


def test_merge_prioritizes_accepted_and_deduplicates():
    assert merge_memory_context(["Approved", "Shared"], ["Shared", "Local"]) == [
        "Approved", "Shared", "Local",
    ]
    approved = [f"Approved {i}" for i in range(12)]
    assert merge_memory_context(approved, ["Local"]) == approved


@pytest.mark.parametrize("bad", [[""], ["x" * 1001], ["x"] * 13, [123]])
def test_merge_rejects_invalid_inputs(bad):
    with pytest.raises(ValueError):
        merge_memory_context(bad, [])


@pytest.mark.asyncio
async def test_selector_binds_identity_parameters_and_validates_result():
    principal = SimpleNamespace(
        tenant_id=uuid4(), subject_id="owner",
        permissions={"conversations:read", "conversations:write"},
    )
    connection = SimpleNamespace(fetch=AsyncMock(return_value=[
        {"content": "Approved"}, {"content": "Approved"},
    ]))
    cid = uuid4()
    assert await select_accepted_memories(connection, principal, cid) == ["Approved"]
    args = connection.fetch.await_args.args
    assert args[1:] == (cid, principal.tenant_id, principal.subject_id)


@pytest.mark.asyncio
async def test_selector_permission_failure_precedes_query():
    connection = SimpleNamespace(fetch=AsyncMock())
    principal = SimpleNamespace(permissions=set())
    with pytest.raises(PermissionError, match="permission denied"):
        await select_accepted_memories(connection, principal, uuid4())
    connection.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_turn_saves_combined_selection(rig, monkeypatch):
    monkeypatch.setenv("ATLAS_ACCEPTED_MEMORY_RECALL", "1")
    selector = AsyncMock(return_value=["Approved", "Shared"])
    monkeypatch.setattr(coordinator, "select_accepted_memories", selector)
    result = await coordinator.reserve_turn(
        rig.pool, rig.principal, rig.cid, rig.mid,
        write_event=rig.event, memory_context=["Shared", "Local"],
    )
    expected = ["Approved", "Shared", "Local"]
    selector.assert_awaited_once_with(
        rig.connection, rig.principal, rig.cid,
    )
    assert stored_memory_context(result) == expected
    assert json.loads(rig.connection.insert_args[6]) == expected
    assert rig.connection.insert_args[7] == memory_context_digest(expected)


@pytest.mark.asyncio
async def test_retry_reuses_snapshot_without_new_selection(rig, monkeypatch):
    monkeypatch.setenv("ATLAS_ACCEPTED_MEMORY_RECALL", "1")
    saved = {"turn_id": uuid4(), "status": "queued", **snapshot(["Saved"])}
    rig.connection.existing = saved
    selector = AsyncMock()
    monkeypatch.setattr(coordinator, "select_accepted_memories", selector)
    result = await coordinator.reserve_turn(
        rig.pool, rig.principal, rig.cid, rig.mid,
        write_event=rig.event, memory_context=["Changed caller input"],
    )
    assert result == saved
    selector.assert_not_awaited()
    assert rig.connection.insert_args is None


@pytest.mark.asyncio
async def test_selector_failure_prevents_turn_creation(rig, monkeypatch):
    monkeypatch.setenv("ATLAS_ACCEPTED_MEMORY_RECALL", "1")
    monkeypatch.setattr(
        coordinator, "select_accepted_memories",
        AsyncMock(side_effect=RuntimeError("recall unavailable")),
    )
    with pytest.raises(RuntimeError, match="recall unavailable"):
        await coordinator.reserve_turn(
            rig.pool, rig.principal, rig.cid, rig.mid,
            write_event=rig.event,
        )
    assert rig.connection.insert_args is None
    rig.event.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_recall_preserves_existing_path(rig, monkeypatch):
    monkeypatch.delenv("ATLAS_ACCEPTED_MEMORY_RECALL", raising=False)
    selector = AsyncMock()
    monkeypatch.setattr(coordinator, "select_accepted_memories", selector)
    result = await coordinator.reserve_turn(
        rig.pool, rig.principal, rig.cid, rig.mid,
        write_event=rig.event, memory_context=["Existing context"],
    )
    assert stored_memory_context(result) == ["Existing context"]
    selector.assert_not_awaited()
