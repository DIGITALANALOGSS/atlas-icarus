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


EVIDENCE_ID = UUID("a187f846-e7fd-4081-9d66-006206df885d")
INTAKE_ID = UUID("197167ec-9d08-4ef7-9750-9dfe7c6b1c7b")
CORRELATION_ID = UUID("9cfcc9ce-c53b-4ab5-a1d4-293f9d07022e")


class FakeConnection:
    def __init__(self, row=None, error=None):
        self.row = row
        self.error = error
        self.calls = []

    async def fetchrow(self, query, evidence_id):
        self.calls.append((query, evidence_id))
        if self.error is not None:
            raise self.error
        return self.row


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
    def _install(row=None, error=None):
        connection = FakeConnection(row=row, error=error)
        pool = FakePool(connection)
        monkeypatch.setattr(app.state, "pool", pool, raising=False)
        return connection, pool

    return _install


@pytest.mark.asyncio
async def test_get_evidence_record_returns_serialized_record(install_pool):
    row = {
        "evidence_id": EVIDENCE_ID,
        "intake_id": INTAKE_ID,
        "sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        "storage_reference": "controlled://atlas-local/evidence/smoke-test-001",
        "media_type": "application/pdf",
        "filename": "synthetic-smoke-test.pdf",
        "description": "Synthetic evidence metadata only; no original file is stored.",
        "metadata": '{"purpose": "endpoint smoke test", "synthetic": true}',
        "correlation_id": CORRELATION_ID,
        "created_at": datetime(2026, 9, 21, 18, 2, 53, 729753, tzinfo=timezone.utc),
    }
    connection, pool = install_pool(row=row)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/evidence-records/{EVIDENCE_ID}")

    assert response.status_code == 200
    assert response.json() == {
        "evidence_id": str(EVIDENCE_ID),
        "intake_id": str(INTAKE_ID),
        "sha256": row["sha256"],
        "storage_reference": row["storage_reference"],
        "media_type": row["media_type"],
        "filename": row["filename"],
        "description": row["description"],
        "metadata": {"purpose": "endpoint smoke test", "synthetic": True},
        "correlation_id": str(CORRELATION_ID),
        "created_at": "2026-09-21T18:02:53.729753Z",
    }
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1
    query, query_evidence_id = connection.calls[0]
    assert "FROM evidence_records" in query
    assert "WHERE evidence_id = $1" in query
    assert query_evidence_id == EVIDENCE_ID


@pytest.mark.asyncio
async def test_get_evidence_record_returns_404_when_missing(install_pool):
    connection, pool = install_pool(row=None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/evidence-records/{EVIDENCE_ID}")

    assert response.status_code == 404
    assert response.json() == {"detail": "evidence record not found"}
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_get_evidence_record_rejects_invalid_uuid(install_pool):
    connection, pool = install_pool()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/evidence-records/not-a-uuid")

    assert response.status_code == 422
    assert connection.calls == []
    assert pool.acquire_count == 0


@pytest.mark.asyncio
async def test_get_evidence_record_returns_503_for_database_failure(install_pool):
    connection, pool = install_pool(error=RuntimeError("database unavailable"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/evidence-records/{EVIDENCE_ID}")

    assert response.status_code == 503
    assert response.json() == {"detail": "database query failed"}
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1
