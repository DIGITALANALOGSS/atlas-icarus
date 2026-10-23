from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from app.auth import Principal
from app import main

def actor(permissions):
    return Principal("test-owner", uuid4(), frozenset(permissions), "user")

@pytest.mark.asyncio
async def test_shared_job_creation_requires_permission_before_sql():
    connection = AsyncMock()
    job = main.JobCreate(
        job_type="metadata.analyze",
        request_payload={"content": "qualification"},
        approval_required=True,
    )
    with pytest.raises(HTTPException) as caught:
        await main._create_job_on_connection(connection, job, actor([]))
    assert caught.value.status_code == 403
    connection.execute.assert_not_called()
    connection.fetchrow.assert_not_called()

@pytest.mark.asyncio
async def test_shared_job_creation_preserves_pending_approval():
    connection = AsyncMock()
    principal = actor(["jobs:create"])
    correlation = uuid4()
    job = main.JobCreate(
        job_type="metadata.analyze",
        request_payload={"content": "qualification"},
        approval_required=True,
        correlation_id=correlation,
    )
    with patch.object(main, "write_event", new_callable=AsyncMock) as events:
        result = await main._create_job_on_connection(
            connection, job, principal
        )
    assert result["status"] == "pending_approval"
    assert result["approval_required"] is True
    assert result["approval_gate_id"] is not None
    assert result["correlation_id"] == str(correlation)
    writes = connection.execute.await_args_list
    assert len(writes) == 2
    assert "INSERT INTO approval_gates" in writes[0].args[0]
    assert writes[0].args[7] == "pending"
    assert "INSERT INTO jobs" in writes[1].args[0]
    assert writes[1].args[4] == "pending_approval"
    assert writes[1].args[5] is True
    assert str(writes[1].args[6]) == result["approval_gate_id"]
    assert writes[1].args[8] == principal.tenant_id
    assert [c.kwargs["event_type"] for c in events.await_args_list] == [
        "governance.approval_gate.created",
        "jobs.created",
        "jobs.approval_requested",
    ]
    connection.transaction.assert_not_called()

@pytest.mark.asyncio
async def test_shared_job_creation_preserves_nonapproval_behavior():
    connection = AsyncMock()
    job = main.JobCreate(
        job_type="metadata.analyze",
        request_payload={"content": "qualification"},
        approval_required=False,
    )
    with patch.object(main, "write_event", new_callable=AsyncMock) as events:
        result = await main._create_job_on_connection(
            connection, job, actor(["jobs:create"])
        )
    assert result["status"] == "queued"
    assert result["approval_gate_id"] is None
    assert connection.execute.await_count == 1
    assert "INSERT INTO jobs" in connection.execute.await_args.args[0]
    assert [c.kwargs["event_type"] for c in events.await_args_list] == [
        "jobs.created", "jobs.queued",
    ]
    connection.transaction.assert_not_called()
