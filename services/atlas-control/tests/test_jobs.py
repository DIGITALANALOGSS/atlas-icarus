from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

import sys

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from app.auth import DEFAULT_TENANT_ID
from app.main import app


DEFAULT_HEADERS = {"Authorization": "Bearer dev-admin"}
WORKER_HEADERS = {"Authorization": "Bearer dev-worker"}

JOB_ID = UUID("a487f846-e7fd-4081-9d66-006206df885d")
MISSING_JOB_ID = UUID("b487f846-e7fd-4081-9d66-006206df885d")
CORRELATION_ID = UUID("d487f846-e7fd-4081-9d66-006206df885d")


class FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeConnection:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []

    def transaction(self):
        self.calls.append(("transaction", None, ()))
        return FakeTransaction()

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if self.rows:
            return self.rows.pop(0)
        return None


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
    def _install(rows=None):
        connection = FakeConnection(rows=rows)
        pool = FakePool(connection)
        monkeypatch.setattr(app.state, "pool", pool, raising=False)
        return connection, pool

    return _install


def job_row():
    return {
        "job_id": JOB_ID,
        "job_type": "research.summarize",
        "request_payload": '{"topic":"tenant isolation"}',
        "status": "queued",
        "approval_required": False,
        "approval_gate_id": None,
        "correlation_id": CORRELATION_ID,
        "result_payload": None,
        "error_code": None,
        "created_at": datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc),
        "started_at": None,
        "completed_at": None,
    }


def executable_job_row(
    status,
    started_at=None,
    completed_at=None,
    result_payload=None,
):
    return {
        "job_id": JOB_ID,
        "job_type": "research.summarize",
        "request_payload": '{"content":"tenant isolation execution"}',
        "status": status,
        "approval_required": False,
        "approval_gate_id": None,
        "correlation_id": CORRELATION_ID,
        "result_payload": result_payload,
        "error_code": None,
        "created_at": datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc),
        "started_at": started_at,
        "completed_at": completed_at,
    }


@pytest.mark.asyncio
async def test_get_job_returns_tenant_scoped_serialized_job(install_pool):
    connection, pool = install_pool(rows=[job_row()])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/jobs/{JOB_ID}", headers=DEFAULT_HEADERS)

    assert response.status_code == 200
    assert response.json() == {
        "job_id": str(JOB_ID),
        "job_type": "research.summarize",
        "request_payload": {"topic": "tenant isolation"},
        "status": "queued",
        "approval_required": False,
        "approval_gate_id": None,
        "correlation_id": str(CORRELATION_ID),
        "result_payload": None,
        "error_code": None,
        "created_at": "2026-09-25T12:00:00Z",
        "started_at": None,
        "completed_at": None,
    }
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1

    method, query, args = connection.calls[0]
    assert method == "fetchrow"
    assert "FROM jobs" in query
    assert "WHERE job_id = $1 AND tenant_id = $2" in query
    assert args == (JOB_ID, DEFAULT_TENANT_ID)


@pytest.mark.asyncio
async def test_get_job_returns_404_when_job_is_not_visible_to_tenant(install_pool):
    connection, pool = install_pool(rows=[None])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            f"/jobs/{MISSING_JOB_ID}",
            headers=DEFAULT_HEADERS,
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "job not found"}
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1

    method, query, args = connection.calls[0]
    assert method == "fetchrow"
    assert "WHERE job_id = $1 AND tenant_id = $2" in query
    assert args == (MISSING_JOB_ID, DEFAULT_TENANT_ID)


@pytest.mark.asyncio
async def test_execute_queued_job_completes_and_writes_events(install_pool):
    started = executable_job_row("running")
    completed = executable_job_row(
        "succeeded",
        result_payload={
            "char_count": len("tenant isolation execution"),
            "word_count": 3,
            "sha256": "98d506d5e949f90c4c637819e4aa144499a97e89d6d0c74ba510839fd1c65a52",
            "analyzed_at": "2026-09-25T12:01:00Z",
        },
    )
    connection, pool = install_pool(rows=[started, completed])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/jobs/{JOB_ID}/execute",
            headers=WORKER_HEADERS,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == str(JOB_ID)
    assert body["status"] == "succeeded"
    assert body["result_payload"]["char_count"] == len("tenant isolation execution")
    assert body["result_payload"]["word_count"] == 3
    assert (
        body["result_payload"]["sha256"]
        == "98d506d5e949f90c4c637819e4aa144499a97e89d6d0c74ba510839fd1c65a52"
    )

    assert pool.acquire_count == 1
    assert connection.calls[0] == ("transaction", None, ())
    assert len(connection.calls) == 5

    start_update = connection.calls[1]
    assert start_update[0] == "fetchrow"
    assert "UPDATE jobs" in start_update[1]
    assert "SET status = 'running'" in start_update[1]
    assert "WHERE job_id = $1" in start_update[1]
    assert "AND tenant_id = $2" in start_update[1]
    assert "AND status = 'queued'" in start_update[1]
    assert start_update[2][0] == JOB_ID
    assert start_update[2][1] == DEFAULT_TENANT_ID
    assert isinstance(start_update[2][2], datetime)
    assert start_update[2][2].tzinfo is not None

    started_event = connection.calls[2]
    assert started_event[0] == "execute"
    assert "INSERT INTO events" in started_event[1]
    assert started_event[2][2] == "jobs.started"
    assert started_event[2][5] == CORRELATION_ID

    completion_update = connection.calls[3]
    assert completion_update[0] == "fetchrow"
    assert "UPDATE jobs" in completion_update[1]
    assert "status = 'succeeded'" in completion_update[1]
    assert "WHERE job_id = $1" in completion_update[1]
    assert "AND tenant_id = $2" in completion_update[1]
    assert "AND status = 'running'" in completion_update[1]
    assert completion_update[2][0] == JOB_ID
    assert completion_update[2][1] == DEFAULT_TENANT_ID
    result_payload = json.loads(completion_update[2][2])
    envelope = json.loads(completion_update[2][4])
    assert envelope["status"] == "succeeded"
    assert envelope["node_id"] == str(JOB_ID)
    assert envelope["workflow_id"] == str(JOB_ID)
    assert envelope["tenant_id"] == str(DEFAULT_TENANT_ID)
    assert envelope["correlation_id"] == str(CORRELATION_ID)
    assert envelope["output"] == result_payload
    assert "workflow_result" not in body
    assert result_payload["char_count"] == len("tenant isolation execution")
    assert result_payload["word_count"] == 3
    assert (
        result_payload["sha256"]
        == "98d506d5e949f90c4c637819e4aa144499a97e89d6d0c74ba510839fd1c65a52"
    )
    assert result_payload["analyzed_at"].endswith("Z")
    assert completion_update[2][3].tzinfo is not None

    succeeded_event = connection.calls[4]
    assert succeeded_event[0] == "execute"
    assert "INSERT INTO events" in succeeded_event[1]
    assert succeeded_event[2][2] == "jobs.succeeded"
    assert succeeded_event[2][5] == CORRELATION_ID


@pytest.mark.asyncio
async def test_execute_running_job_returns_conflict_without_duplicate_writes(
    install_pool,
):
    running = executable_job_row(
        "running",
        started_at=datetime(2026, 9, 25, 12, 1, 0, tzinfo=timezone.utc),
    )
    connection, pool = install_pool(rows=[None, running])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/jobs/{JOB_ID}/execute",
            headers=WORKER_HEADERS,
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "job is already running"}
    assert pool.acquire_count == 1
    assert connection.calls[0] == ("transaction", None, ())
    assert len(connection.calls) == 3

    queued_update = connection.calls[1]
    assert queued_update[0] == "fetchrow"
    assert "UPDATE jobs" in queued_update[1]
    assert "AND tenant_id = $2" in queued_update[1]
    assert "AND status = 'queued'" in queued_update[1]
    assert queued_update[2][0] == JOB_ID
    assert queued_update[2][1] == DEFAULT_TENANT_ID

    existing_lookup = connection.calls[2]
    assert existing_lookup[0] == "fetchrow"
    assert "FROM jobs" in existing_lookup[1]
    assert "WHERE job_id = $1 AND tenant_id = $2" in existing_lookup[1]
    assert existing_lookup[2] == (JOB_ID, DEFAULT_TENANT_ID)

    assert not any(call[0] == "execute" for call in connection.calls)


@pytest.mark.asyncio
async def test_execute_succeeded_job_returns_stored_result_without_duplicate_writes(
    install_pool,
):
    completed_at = datetime(2026, 9, 25, 12, 2, 0, tzinfo=timezone.utc)
    succeeded = executable_job_row(
        "succeeded",
        started_at=datetime(2026, 9, 25, 12, 1, 0, tzinfo=timezone.utc),
        completed_at=completed_at,
        result_payload={
            "char_count": len("tenant isolation execution"),
            "word_count": 3,
            "sha256": "98d506d5e949f90c4c637819e4aa144499a97e89d6d0c74ba510839fd1c65a52",
            "analyzed_at": "2026-09-25T12:02:00Z",
        },
    )
    connection, pool = install_pool(rows=[None, succeeded])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/jobs/{JOB_ID}/execute",
            headers=WORKER_HEADERS,
        )

    assert response.status_code == 200
    assert response.json() == {
        "job_id": str(JOB_ID),
        "job_type": "research.summarize",
        "request_payload": {"content": "tenant isolation execution"},
        "status": "succeeded",
        "approval_required": False,
        "approval_gate_id": None,
        "correlation_id": str(CORRELATION_ID),
        "result_payload": {
            "char_count": len("tenant isolation execution"),
            "word_count": 3,
            "sha256": "98d506d5e949f90c4c637819e4aa144499a97e89d6d0c74ba510839fd1c65a52",
            "analyzed_at": "2026-09-25T12:02:00Z",
        },
        "error_code": None,
        "created_at": "2026-09-25T12:00:00Z",
        "started_at": "2026-09-25T12:01:00Z",
        "completed_at": "2026-09-25T12:02:00Z",
    }
    assert pool.acquire_count == 1
    assert connection.calls[0] == ("transaction", None, ())
    assert len(connection.calls) == 3

    queued_update = connection.calls[1]
    assert queued_update[0] == "fetchrow"
    assert "UPDATE jobs" in queued_update[1]
    assert "AND tenant_id = $2" in queued_update[1]
    assert "AND status = 'queued'" in queued_update[1]
    assert queued_update[2][0] == JOB_ID
    assert queued_update[2][1] == DEFAULT_TENANT_ID

    existing_lookup = connection.calls[2]
    assert existing_lookup[0] == "fetchrow"
    assert "SELECT" in existing_lookup[1]
    assert "result_payload" in existing_lookup[1]
    assert "FROM jobs" in existing_lookup[1]
    assert "WHERE job_id = $1 AND tenant_id = $2" in existing_lookup[1]
    assert existing_lookup[2] == (JOB_ID, DEFAULT_TENANT_ID)

    assert not any(call[0] == "execute" for call in connection.calls)


@pytest.mark.asyncio
async def test_execute_job_returns_404_without_writes_when_not_visible_to_tenant(
    install_pool,
):
    connection, pool = install_pool(rows=[None, None])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/jobs/{MISSING_JOB_ID}/execute",
            headers=WORKER_HEADERS,
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "job not found"}
    assert pool.acquire_count == 1
    assert connection.calls[0] == ("transaction", None, ())
    assert len(connection.calls) == 3

    queued_update = connection.calls[1]
    assert queued_update[0] == "fetchrow"
    assert "UPDATE jobs" in queued_update[1]
    assert "WHERE job_id = $1" in queued_update[1]
    assert "AND tenant_id = $2" in queued_update[1]
    assert "AND status = 'queued'" in queued_update[1]
    assert queued_update[2][0] == MISSING_JOB_ID
    assert queued_update[2][1] == DEFAULT_TENANT_ID

    visibility_lookup = connection.calls[2]
    assert visibility_lookup[0] == "fetchrow"
    assert "SELECT" in visibility_lookup[1]
    assert "status" in visibility_lookup[1]
    assert "WHERE job_id = $1 AND tenant_id = $2" in visibility_lookup[1]
    assert visibility_lookup[2] == (MISSING_JOB_ID, DEFAULT_TENANT_ID)

    assert not any(call[0] == "execute" for call in connection.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
async def test_adapter_failure_records_failed_job(
    install_pool, monkeypatch, error_type
):
    from app import main as main_module

    started = executable_job_row("running")
    failed = executable_job_row(
        "failed", completed_at=datetime.now(timezone.utc)
    )
    failed["error_code"] = "analysis_execution_failed"
    connection, pool = install_pool(rows=[started, failed])

    def broken_adapter(*args, **kwargs):
        raise error_type("private exception detail")

    monkeypatch.setattr(
        main_module, "execute_text_analysis", broken_adapter
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post(
            f"/jobs/{JOB_ID}/execute", headers=WORKER_HEADERS
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_code"] == "analysis_execution_failed"
    assert body["result_payload"] is None
    assert body["completed_at"] is not None
    assert "private exception detail" not in response.text
    assert pool.acquire_count == 1
    assert len(connection.calls) == 5

    update = connection.calls[3]
    assert update[0] == "fetchrow"
    assert "status = 'failed'" in update[1]
    assert "result_payload = NULL" in update[1]
    assert "AND tenant_id = $2" in update[1]
    assert "AND status = 'running'" in update[1]
    assert update[2][:3] == (
        JOB_ID, DEFAULT_TENANT_ID, "analysis_execution_failed"
    )
    assert update[2][3].tzinfo is not None
    envelope = json.loads(update[2][4])
    assert envelope["status"] == "failed"
    assert envelope["workflow_id"] == str(JOB_ID)
    assert envelope["tenant_id"] == str(DEFAULT_TENANT_ID)
    assert envelope["correlation_id"] == str(CORRELATION_ID)
    assert envelope["error"]["code"] == "analysis_execution_failed"
    assert "private exception detail" not in update[2][4]
    assert "workflow_result" not in body

    events = [
        call for call in connection.calls
        if call[0] == "execute" and "INSERT INTO events" in call[1]
    ]
    assert [call[2][2] for call in events] == [
        "jobs.started", "jobs.failed"
    ]
    assert all(call[2][5] == CORRELATION_ID for call in events)
    assert "private exception detail" not in repr(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("database_failure", [False, True])
async def test_failure_update_conflict_or_database_error(
    install_pool, monkeypatch, database_failure
):
    from app import main as main_module

    connection, _ = install_pool(
        rows=[executable_job_row("running"), None]
    )

    def broken_adapter(*args, **kwargs):
        raise RuntimeError("private adapter detail")

    monkeypatch.setattr(
        main_module, "execute_text_analysis", broken_adapter
    )
    if database_failure:
        original_fetchrow = connection.fetchrow

        async def failing_fetchrow(query, *args):
            if "status = 'failed'" in query:
                raise RuntimeError("private database detail")
            return await original_fetchrow(query, *args)

        monkeypatch.setattr(connection, "fetchrow", failing_fetchrow)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post(
            f"/jobs/{JOB_ID}/execute", headers=WORKER_HEADERS
        )

    assert response.status_code == (503 if database_failure else 409)
    assert "private" not in response.text
    events = [
        call for call in connection.calls
        if call[0] == "execute" and "INSERT INTO events" in call[1]
    ]
    assert [call[2][2] for call in events] == ["jobs.started"]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
async def test_read_persisted_workflow_result(install_pool, outcome):
    envelope = {
        "result_id": str(MISSING_JOB_ID),
        "node_id": str(JOB_ID),
        "workflow_id": str(JOB_ID),
        "correlation_id": str(CORRELATION_ID),
        "causation_id": None,
        "tenant_id": str(DEFAULT_TENANT_ID),
        "status": outcome,
        "output": {"word_count": 3} if outcome == "succeeded" else {},
        "sources": [],
        "error": (
            {
                "code": "analysis_execution_failed",
                "message": "Text analysis could not be completed.",
                "retryable": False,
                "detail": {},
            }
            if outcome == "failed" else None
        ),
        "completed_at": "2026-10-07T12:00:00Z",
    }
    connection, pool = install_pool(rows=[{
        "job_id": JOB_ID,
        "workflow_result": json.dumps(envelope),
    }])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get(
            f"/jobs/{JOB_ID}/workflow-result",
            headers=DEFAULT_HEADERS,
        )
    assert response.status_code == 200, response.text
    assert response.json() == envelope
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1
    query = connection.calls[0]
    assert query[0] == "fetchrow"
    assert "workflow_result" in query[1]
    assert "WHERE job_id = $1 AND tenant_id = $2" in query[1]
    assert query[2] == (JOB_ID, DEFAULT_TENANT_ID)


@pytest.mark.asyncio
async def test_workflow_result_not_available(install_pool):
    install_pool(rows=[{"job_id": JOB_ID, "workflow_result": None}])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get(
            f"/jobs/{JOB_ID}/workflow-result",
            headers=DEFAULT_HEADERS,
        )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "workflow result is not available"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token", ["dev-admin", "dev-cross-tenant-operator"]
)
async def test_workflow_result_missing_or_other_tenant(install_pool, token):
    connection, _ = install_pool(rows=[None])
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get(
            f"/jobs/{JOB_ID}/workflow-result",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 404
    assert response.json() == {"detail": "job not found"}
    assert "AND tenant_id = $2" in connection.calls[0][1]


@pytest.mark.asyncio
async def test_workflow_result_requires_authentication(install_pool):
    connection, _ = install_pool()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get(
            f"/jobs/{JOB_ID}/workflow-result"
        )
    assert response.status_code == 401
    assert connection.calls == []


@pytest.mark.asyncio
async def test_workflow_result_database_error(install_pool, monkeypatch):
    connection, _ = install_pool()

    async def broken_read(*args, **kwargs):
        raise RuntimeError("private database detail")

    monkeypatch.setattr(connection, "fetchrow", broken_read)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get(
            f"/jobs/{JOB_ID}/workflow-result",
            headers=DEFAULT_HEADERS,
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "database read failed"}
    assert "private database detail" not in response.text
