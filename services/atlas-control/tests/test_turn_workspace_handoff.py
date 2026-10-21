import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.conversation_coordinator as coordinator
from app.conversation_turn_routes import ConversationTurnCreate
from app.conversation_workspace_bindings import WorkspaceBindingError
from test_turn_memory_snapshots import rig


def test_request_preserves_omitted_versus_explicit_personal():
    omitted = ConversationTurnCreate(user_message_id=uuid4())
    personal = ConversationTurnCreate(
        user_message_id=uuid4(), workspace_project_id=None,
    )
    assert "workspace_project_id" not in omitted.model_fields_set
    assert "workspace_project_id" in personal.model_fields_set
    assert personal.workspace_project_id is None


def test_request_parses_project_uuid():
    project = uuid4()
    request = ConversationTurnCreate(
        user_message_id=uuid4(), workspace_project_id=str(project),
    )
    assert request.workspace_project_id == project


@pytest.mark.asyncio
async def test_omitted_workspace_does_not_call_binding(rig, monkeypatch):
    binding = AsyncMock()
    monkeypatch.setattr(coordinator, "ensure_workspace_binding", binding)
    await coordinator.reserve_turn(
        rig.pool, rig.principal, rig.cid, rig.mid,
        write_event=rig.event,
    )
    binding.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("project", [None, uuid4()])
async def test_explicit_workspace_reaches_binding(rig, monkeypatch, project):
    binding = AsyncMock(return_value={})
    monkeypatch.setattr(coordinator, "ensure_workspace_binding", binding)
    await coordinator.reserve_turn(
        rig.pool, rig.principal, rig.cid, rig.mid,
        write_event=rig.event, workspace_project_id=project,
    )
    binding.assert_awaited_once_with(
        rig.connection, rig.principal, rig.cid, project,
        write_event=rig.event,
    )
    assert rig.connection.insert_args is not None


@pytest.mark.asyncio
async def test_binding_conflict_prevents_turn_insert(rig, monkeypatch):
    binding = AsyncMock(side_effect=WorkspaceBindingError(
        "conversation workspace binding changed"
    ))
    monkeypatch.setattr(coordinator, "ensure_workspace_binding", binding)
    with pytest.raises(
        coordinator.ConversationCoordinatorError, match="binding changed"
    ):
        await coordinator.reserve_turn(
            rig.pool, rig.principal, rig.cid, rig.mid,
            write_event=rig.event, workspace_project_id=uuid4(),
        )
    assert rig.connection.insert_args is None
    rig.event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("project", [None, uuid4()])
async def test_execute_forwards_explicit_workspace(rig, monkeypatch, project):
    completed = {
        "turn_id": uuid4(),
        "status": "completed",
        "assistant_message_id": rig.connection.completed_message["message_id"],
    }
    reserve = AsyncMock(return_value=completed)
    monkeypatch.setattr(coordinator, "reserve_turn", reserve)
    model = SimpleNamespace(complete=AsyncMock())
    await coordinator.execute_turn(
        rig.pool, rig.principal, rig.cid, rig.mid,
        model=model, write_event=rig.event,
        workspace_project_id=project,
    )
    assert reserve.await_args.kwargs["workspace_project_id"] == project
    model.complete.assert_not_awaited()
