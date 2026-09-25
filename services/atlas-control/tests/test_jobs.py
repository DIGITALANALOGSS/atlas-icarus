from contextlib import asynccontextmanager
from datetime import datetime, timezone
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

JOB_ID = UUID("a487f846-e7fd-4081-9d66-006206df885d")
MISSING_JOB_ID = UUID("b487f846-e7fd-4081-9d66-006206df885d")
CORRELATION_ID = UUID("d487f846-e7fd-4081-9d66-006206df885d")


class FakeConnection:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []

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
