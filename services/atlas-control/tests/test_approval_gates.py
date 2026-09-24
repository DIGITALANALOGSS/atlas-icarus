from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest

import sys

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from app.main import app

DEFAULT_HEADERS = {"Authorization": "Bearer dev-admin"}
REQUESTER_HEADERS = {"Authorization": "Bearer dev-requester"}
APPROVER_HEADERS = {"Authorization": "Bearer dev-approver"}
WORKER_HEADERS = {"Authorization": "Bearer dev-worker"}


GATE_ID = UUID("c187f846-e7fd-4081-9d66-006206df885d")
SECOND_GATE_ID = UUID("d287f846-e7fd-4081-9d66-006206df885d")
MISSING_GATE_ID = UUID("e387f846-e7fd-4081-9d66-006206df885d")
EVENT_ID = UUID("f487f846-e7fd-4081-9d66-006206df885d")
CORRELATION_ID = UUID("9cfcc9ce-c53b-4ab5-a1d4-293f9d07022e")
JOB_ID = UUID("a487f846-e7fd-4081-9d66-006206df885d")


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, rows=None, error=None):
        self.rows = list(rows or [])
        self.error = error
        self.calls = []

    def transaction(self):
        self.calls.append(("transaction", None, ()))
        return FakeTransaction()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if self.error is not None:
            raise self.error

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if self.error is not None:
            raise self.error
        if self.rows:
            return self.rows.pop(0)
        return None

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if self.error is not None:
            raise self.error
        if self.rows:
            return self.rows.pop(0)
        return []


class FakePool:
    def __init__(self, connection):
        self.connection = connection
        self.acquire_count = 0

    @asynccontextmanager
    async def acquire(self):
        self.acquire_count += 1
        yield self.connection


@pytest.fixture
def install_pool(monkeypatch):
    def _install(rows=None, error=None):
        connection = FakeConnection(rows=rows, error=error)
        pool = FakePool(connection)
        monkeypatch.setattr(app.state, "pool", pool, raising=False)
        return connection, pool

    return _install


def gate_row(
    gate_id=GATE_ID,
    gate_status="pending",
    created_at=datetime(2026, 9, 23, 13, 0, 0, tzinfo=timezone.utc),
    decided_at=None,
    decided_by=None,
    decision_reason=None,
    decision_event_id=None,
):
    return {
        "gate_id": gate_id,
        "requester": "atlas-engineering-agent",
        "action_type": "engineering.patch.apply",
        "risk_level": "high",
        "action_payload": '{"repository":"atlas-icarus","branch":"main","files":["app/main.py"]}',
        "summary": "Apply the reviewed Approval Gates implementation.",
        "status": gate_status,
        "correlation_id": CORRELATION_ID,
        "created_at": created_at,
        "decided_at": decided_at,
        "decided_by": decided_by,
        "decision_reason": decision_reason,
        "decision_event_id": decision_event_id,
    }


def create_payload():
    return {
        "requester": "  atlas-engineering-agent  ",
        "action_type": "engineering.patch.apply",
        "risk_level": "HIGH",
        "action_payload": {
            "repository": "atlas-icarus",
            "branch": "main",
            "files": ["app/main.py"],
        },
        "summary": "  Apply the reviewed Approval Gates implementation.  ",
        "correlation_id": str(CORRELATION_ID),
    }


def job_row(status, completed_at=None):
    return {
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "status": status,
        "completed_at": completed_at,
    }


@pytest.mark.asyncio
async def test_create_approval_gate_persists_pending_gate_and_event(install_pool):
    connection, pool = install_pool()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/approval-gates", json=create_payload(), headers=REQUESTER_HEADERS)

    assert response.status_code == 201
    body = response.json()
    assert body["requester"] == "atlas-engineering-agent"
    assert body["action_type"] == "engineering.patch.apply"
    assert body["risk_level"] == "high"
    assert body["action_payload"]["repository"] == "atlas-icarus"
    assert body["summary"] == "Apply the reviewed Approval Gates implementation."
    assert body["status"] == "pending"
    assert body["correlation_id"] == str(CORRELATION_ID)
    assert body["event_type"] == "governance.approval_gate.created"
    assert UUID(body["gate_id"])
    assert UUID(body["event_id"])

    assert pool.acquire_count == 1
    assert connection.calls[0] == ("transaction", None, ())
    execute_calls = [call for call in connection.calls if call[0] == "execute"]
    assert len(execute_calls) == 2
    assert "INSERT INTO approval_gates" in execute_calls[0][1]
    assert execute_calls[0][2][1] == "atlas-engineering-agent"
    assert execute_calls[0][2][2] == "engineering.patch.apply"
    assert execute_calls[0][2][3] == "high"
    assert execute_calls[0][2][6] == "pending"
    assert execute_calls[0][2][7] == CORRELATION_ID
    assert "INSERT INTO events" in execute_calls[1][1]
    assert execute_calls[1][2][1] == "governance.approval_gate.created"
    assert execute_calls[1][2][4] == CORRELATION_ID


@pytest.mark.asyncio
async def test_get_approval_gate_returns_serialized_gate(install_pool):
    row = gate_row()
    connection, pool = install_pool(rows=[row])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/approval-gates/{GATE_ID}", headers=DEFAULT_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "gate_id": str(GATE_ID),
        "requester": "atlas-engineering-agent",
        "action_type": "engineering.patch.apply",
        "risk_level": "high",
        "action_payload": {
            "repository": "atlas-icarus",
            "branch": "main",
            "files": ["app/main.py"],
        },
        "summary": "Apply the reviewed Approval Gates implementation.",
        "status": "pending",
        "correlation_id": str(CORRELATION_ID),
        "created_at": "2026-09-23T13:00:00Z",
        "decided_at": None,
        "decided_by": None,
        "decision_reason": None,
        "decision_event_id": None,
    }
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1
    method, query, args = connection.calls[0]
    assert method == "fetchrow"
    assert "FROM approval_gates" in query
    assert "WHERE gate_id = $1" in query
    assert args == (GATE_ID,)


@pytest.mark.asyncio
async def test_get_approval_gate_returns_404_when_missing(install_pool):
    connection, pool = install_pool(rows=[None])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/approval-gates/{MISSING_GATE_ID}", headers=DEFAULT_HEADERS)

    assert response.status_code == 404
    assert response.json() == {"detail": "approval gate not found"}
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_list_approval_gates_filters_by_status(install_pool):
    first = gate_row()
    second = gate_row(
        gate_id=SECOND_GATE_ID,
        created_at=datetime(2026, 9, 22, 13, 0, 0, tzinfo=timezone.utc),
    )
    connection, pool = install_pool(rows=[[first, second]])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/approval-gates?status=pending&limit=2&offset=1", headers=DEFAULT_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert [item["gate_id"] for item in body["items"]] == [
        str(GATE_ID),
        str(SECOND_GATE_ID),
    ]

    assert pool.acquire_count == 1
    assert len(connection.calls) == 1
    method, query, args = connection.calls[0]
    assert method == "fetch"
    assert "FROM approval_gates" in query
    assert "WHERE status = $1" in query
    assert "ORDER BY created_at DESC, gate_id DESC" in query
    assert "LIMIT $2 OFFSET $3" in query
    assert args == ("pending", 2, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/approval-gates/not-a-uuid",
        "/approval-gates?status=unknown",
        "/approval-gates?limit=0",
        "/approval-gates?limit=101",
        "/approval-gates?offset=-1",
    ],
)
async def test_approval_gate_routes_reject_invalid_parameters(install_pool, path):
    connection, pool = install_pool()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(path, headers=DEFAULT_HEADERS)

    assert response.status_code == 422
    assert connection.calls == []
    assert pool.acquire_count == 0


@pytest.mark.asyncio
async def test_create_approval_gate_rejects_invalid_payload(install_pool):
    connection, pool = install_pool()
    payload = create_payload()
    payload["action_type"] = "not valid"
    payload["risk_level"] = "urgent"

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/approval-gates", json=payload, headers=REQUESTER_HEADERS)

    assert response.status_code == 422
    assert connection.calls == []
    assert pool.acquire_count == 0


@pytest.mark.asyncio
async def test_approve_pending_gate_queues_linked_job_and_writes_events(install_pool):
    decided_at = datetime(2026, 9, 23, 13, 5, 0, tzinfo=timezone.utc)
    gate = gate_row(
        gate_status="approved",
        decided_at=decided_at,
        decided_by="freedome",
        decision_reason="Tests passed and the change is reviewed.",
        decision_event_id=EVENT_ID,
    )
    connection, pool = install_pool(rows=[gate, job_row("queued")])

    payload = {
        "decided_by": "  freedome  ",
        "decision_reason": "  Tests passed and the change is reviewed.  ",
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/approval-gates/{GATE_ID}/approve",
            json=payload,
            headers=APPROVER_HEADERS,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == "freedome"
    assert body["decision_reason"] == "Tests passed and the change is reviewed."
    assert body["decision_event_id"] == str(EVENT_ID)
    assert UUID(body["event_id"])
    assert body["event_id"] != body["decision_event_id"]
    assert body["event_type"] == "governance.approval_gate.approved"
    assert body["job_id"] == str(JOB_ID)
    assert body["job_status"] == "queued"
    assert body["job_event_type"] == "jobs.queued"

    assert pool.acquire_count == 1
    assert connection.calls[0] == ("transaction", None, ())

    gate_update = connection.calls[1]
    assert gate_update[0] == "fetchrow"
    assert "UPDATE approval_gates" in gate_update[1]
    assert "WHERE gate_id = $1 AND status = 'pending'" in gate_update[1]
    assert gate_update[2][0] == GATE_ID
    assert gate_update[2][1] == "approved"

    approval_event = connection.calls[2]
    assert approval_event[0] == "execute"
    assert "INSERT INTO events" in approval_event[1]
    assert approval_event[2][1] == "governance.approval_gate.approved"
    assert approval_event[2][4] == CORRELATION_ID

    job_update = connection.calls[3]
    assert job_update[0] == "fetchrow"
    assert "UPDATE jobs" in job_update[1]
    assert "WHERE approval_gate_id = $1" in job_update[1]
    assert "AND status = 'pending_approval'" in job_update[1]
    assert job_update[2] == (GATE_ID, "queued", None)

    job_event = connection.calls[4]
    assert job_event[0] == "execute"
    assert "INSERT INTO events" in job_event[1]
    assert job_event[2][1] == "jobs.queued"
    assert job_event[2][4] == CORRELATION_ID


@pytest.mark.asyncio
async def test_reject_pending_gate_rejects_linked_job_and_writes_events(install_pool):
    decided_at = datetime(2026, 9, 23, 13, 6, 0, tzinfo=timezone.utc)
    gate = gate_row(
        gate_status="rejected",
        decided_at=decided_at,
        decided_by="freedome",
        decision_reason=None,
        decision_event_id=EVENT_ID,
    )
    connection, pool = install_pool(rows=[gate, job_row("rejected", decided_at)])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/approval-gates/{GATE_ID}/reject",
            json={"decided_by": "freedome"},
            headers=APPROVER_HEADERS,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["decided_by"] == "freedome"
    assert body["decision_reason"] is None
    assert body["event_type"] == "governance.approval_gate.rejected"
    assert body["job_id"] == str(JOB_ID)
    assert body["job_status"] == "rejected"
    assert body["job_event_type"] == "jobs.rejected"

    assert pool.acquire_count == 1
    assert connection.calls[0] == ("transaction", None, ())

    gate_update = connection.calls[1]
    assert gate_update[0] == "fetchrow"
    assert "UPDATE approval_gates" in gate_update[1]
    assert gate_update[2][1] == "rejected"

    approval_event = connection.calls[2]
    assert approval_event[0] == "execute"
    assert "INSERT INTO events" in approval_event[1]
    assert approval_event[2][1] == "governance.approval_gate.rejected"
    assert approval_event[2][4] == CORRELATION_ID

    job_update = connection.calls[3]
    assert job_update[0] == "fetchrow"
    assert "UPDATE jobs" in job_update[1]
    assert "WHERE approval_gate_id = $1" in job_update[1]
    assert "AND status = 'pending_approval'" in job_update[1]
    assert job_update[2][0] == GATE_ID
    assert job_update[2][1] == "rejected"
    assert isinstance(job_update[2][2], datetime)
    assert job_update[2][2].tzinfo is not None

    job_event = connection.calls[4]
    assert job_event[0] == "execute"
    assert "INSERT INTO events" in job_event[1]
    assert job_event[2][1] == "jobs.rejected"
    assert job_event[2][4] == CORRELATION_ID


@pytest.mark.asyncio
async def test_decision_returns_404_when_gate_does_not_exist(install_pool):
    connection, pool = install_pool(rows=[None, None])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/approval-gates/{MISSING_GATE_ID}/approve",
            json={"decided_by": "freedome"},
            headers=APPROVER_HEADERS,
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "approval gate not found"}
    assert pool.acquire_count == 1
    assert connection.calls[0] == ("transaction", None, ())
    assert connection.calls[1][0] == "fetchrow"
    assert connection.calls[2][0] == "fetchrow"


@pytest.mark.asyncio
async def test_decision_returns_409_when_gate_is_not_pending(install_pool):
    connection, pool = install_pool(rows=[None, {"status": "approved"}])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/approval-gates/{GATE_ID}/reject",
            json={"decided_by": "freedome"},
            headers=APPROVER_HEADERS,
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "approval gate is no longer pending"}
    assert pool.acquire_count == 1
    assert connection.calls[0] == ("transaction", None, ())
    assert connection.calls[1][0] == "fetchrow"
    assert connection.calls[2][0] == "fetchrow"


@pytest.mark.asyncio
async def test_second_approval_of_same_gate_returns_409(install_pool):
    decided_at = datetime(2026, 9, 23, 13, 7, 0, tzinfo=timezone.utc)
    approved_gate = gate_row(
        gate_status="approved",
        decided_at=decided_at,
        decided_by="freedome",
        decision_reason="Approved once.",
        decision_event_id=EVENT_ID,
    )
    connection, pool = install_pool(
        rows=[
            approved_gate,
            job_row("queued"),
            None,
            {"status": "approved"},
        ]
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/approval-gates/{GATE_ID}/approve",
            json={
                "decided_by": "freedome",
                "decision_reason": "Approved once.",
            },
            headers=APPROVER_HEADERS,
        )
        second = await client.post(
            f"/approval-gates/{GATE_ID}/approve",
            json={
                "decided_by": "freedome",
                "decision_reason": "Attempted duplicate approval.",
            },
            headers=APPROVER_HEADERS,
        )

    assert first.status_code == 200
    assert first.json()["status"] == "approved"
    assert first.json()["job_status"] == "queued"

    assert second.status_code == 409
    assert second.json() == {"detail": "approval gate is no longer pending"}

    assert pool.acquire_count == 2
    assert connection.calls[0] == ("transaction", None, ())
    assert connection.calls[5] == ("transaction", None, ())
    assert connection.calls[6][0] == "fetchrow"
    assert "UPDATE approval_gates" in connection.calls[6][1]
    assert connection.calls[7][0] == "fetchrow"
    assert "SELECT status FROM approval_gates" in connection.calls[7][1]


@pytest.mark.asyncio
async def test_approval_gate_returns_503_for_database_failure(install_pool):
    connection, pool = install_pool(error=RuntimeError("database unavailable"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/approval-gates/{GATE_ID}", headers=DEFAULT_HEADERS)

    assert response.status_code == 503
    assert response.json() == {"detail": "database query failed"}
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1
