import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.conversation_memory import memory_context_digest
from app.conversation_memory_lifecycle import (
    MemoryLifecycleError, dependency_digest, snapshot_records,
    validate_turn_dependencies,
)
from app.conversation_memory_lifecycle_routes import (
    MemoryWithdrawal, MemorySupersession,
)
from app.conversation_memory_review_routes import (
    register_conversation_memory_review_routes,
)


@pytest.fixture
def case(monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_LIFECYCLE_ENFORCEMENT", "1")
    principal = SimpleNamespace(
        tenant_id=uuid4(), subject_id="lifecycle-test-owner",
        permissions={"conversations:read", "conversations:write"},
    )
    pid = uuid4()
    memories, dependencies = snapshot_records([
        {"proposal_id": pid, "content": "Approved preference"},
    ])
    turn = {
        "conversation_id": uuid4(),
        "memory_context": json.dumps(memories),
        "memory_context_sha256": memory_context_digest(memories),
        "memory_dependencies": json.dumps(dependencies),
        "memory_dependencies_sha256": dependency_digest(dependencies),
    }
    connection = SimpleNamespace(
        fetchrow=AsyncMock(return_value={"content": "Approved preference"}),
    )
    return SimpleNamespace(
        principal=principal, pid=pid, turn=turn, connection=connection,
    )


def test_snapshot_deduplicates_content_and_preserves_selected_id():
    first, second = uuid4(), uuid4()
    memories, dependencies = snapshot_records([
        {"proposal_id": first, "content": "Same"},
        {"proposal_id": second, "content": "Same"},
    ])
    assert memories == ["Same"]
    assert len(dependencies) == 1
    assert dependencies[0]["proposal_id"] == str(first)


def test_dependency_digest_binds_record_order():
    a = {"proposal_id": str(uuid4()), "content_sha256": "a" * 64}
    b = {"proposal_id": str(uuid4()), "content_sha256": "b" * 64}
    assert dependency_digest([a, b]) != dependency_digest([b, a])


@pytest.mark.asyncio
async def test_valid_dependency_reaches_owner_scoped_query(case):
    await validate_turn_dependencies(
        case.connection, case.principal, case.turn,
    )
    args = case.connection.fetchrow.await_args.args
    assert args[1:] == (
        case.pid, case.principal.tenant_id,
        case.principal.subject_id, case.turn["conversation_id"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "changed", "digest", "legacy"])
async def test_invalid_dependency_is_rejected(case, mode):
    if mode == "missing":
        case.connection.fetchrow.return_value = None
    elif mode == "changed":
        case.connection.fetchrow.return_value = {"content": "Changed"}
    elif mode == "digest":
        case.turn["memory_dependencies_sha256"] = "0" * 64
    else:
        case.turn["memory_dependencies"] = None
        case.turn["memory_dependencies_sha256"] = None
    with pytest.raises(MemoryLifecycleError):
        await validate_turn_dependencies(
            case.connection, case.principal, case.turn,
        )
    if mode in {"digest", "legacy"}:
        case.connection.fetchrow.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabling_enforcement_does_not_allow_linked_snapshot(case, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_LIFECYCLE_ENFORCEMENT", "0")
    with pytest.raises(MemoryLifecycleError, match="enforcement required"):
        await validate_turn_dependencies(
            case.connection, case.principal, case.turn,
        )
    case.connection.fetchrow.assert_not_awaited()


@pytest.mark.parametrize("reason", ["", " ", "x" * 1001])
def test_invalid_reason_is_rejected(reason):
    with pytest.raises(ValueError):
        MemoryWithdrawal(reason=reason)


def test_supersession_requires_replacement():
    with pytest.raises(ValueError):
        MemorySupersession(reason="Correct an outdated record")


def test_lifecycle_routes_register_once_through_review_registrar():
    app = FastAPI()
    register_conversation_memory_review_routes(app, AsyncMock())
    for path in (
        "/memory-proposals/{proposal_id}/revoke",
        "/memory-proposals/{proposal_id}/supersede",
    ):
        matches = [
            route for route in app.routes
            if getattr(route, "path", None) == path
            and "POST" in getattr(route, "methods", set())
        ]
        assert len(matches) == 1
    assert app.openapi()["paths"][
        "/memory-proposals/{proposal_id}/revoke"
    ]["post"]
