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


EVIDENCE_ID = UUID("a187f846-e7fd-4081-9d66-006206df885d")
SECOND_EVIDENCE_ID = UUID("b287f846-e7fd-4081-9d66-006206df885d")
INTAKE_ID = UUID("197167ec-9d08-4ef7-9750-9dfe7c6b1c7b")
MISSING_INTAKE_ID = UUID("297167ec-9d08-4ef7-9750-9dfe7c6b1c7b")
CORRELATION_ID = UUID("9cfcc9ce-c53b-4ab5-a1d4-293f9d07022e")


class FakeConnection:
    def __init__(self, row=None, rows=None, error=None):
        self.row = row
        self.rows = rows or []
        self.error = error
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if self.error is not None:
            raise self.error
        return self.row

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if self.error is not None:
            raise self.error
        return self.rows


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
    def _install(row=None, rows=None, error=None):
        connection = FakeConnection(row=row, rows=rows, error=error)
        pool = FakePool(connection)
        monkeypatch.setattr(app.state, "pool", pool, raising=False)
        return connection, pool

    return _install


def evidence_row(
    evidence_id=EVIDENCE_ID,
    created_at=datetime(2026, 9, 21, 18, 2, 53, 729753, tzinfo=timezone.utc),
):
    return {
        "evidence_id": evidence_id,
        "intake_id": INTAKE_ID,
        "sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
        "storage_reference": "controlled://atlas-local/evidence/smoke-test-001",
        "media_type": "application/pdf",
        "filename": "synthetic-smoke-test.pdf",
        "description": "Synthetic evidence metadata only; no original file is stored.",
        "metadata": '{"purpose": "endpoint smoke test", "synthetic": true}',
        "correlation_id": CORRELATION_ID,
        "created_at": created_at,
    }


@pytest.mark.asyncio
async def test_get_evidence_record_returns_serialized_record(install_pool):
    row = evidence_row()
    connection, pool = install_pool(row=row)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer dev-requester"},
    ) as client:
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
    method, query, args = connection.calls[0]
    assert method == "fetchrow"
    assert "FROM evidence_records" in query
    assert "WHERE evidence_id = $1" in query
    assert "AND tenant_id = $2" in query
    assert args == (EVIDENCE_ID, DEFAULT_TENANT_ID)


@pytest.mark.asyncio
async def test_get_evidence_record_returns_404_when_missing(install_pool):
    connection, pool = install_pool(row=None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer dev-requester"},
    ) as client:
        response = await client.get(f"/evidence-records/{EVIDENCE_ID}")

    assert response.status_code == 404
    assert response.json() == {"detail": "evidence record not found"}
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_get_evidence_record_rejects_invalid_uuid(install_pool):
    connection, pool = install_pool()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer dev-requester"},
    ) as client:
        response = await client.get("/evidence-records/not-a-uuid")

    assert response.status_code == 422
    assert connection.calls == []
    assert pool.acquire_count == 0


@pytest.mark.asyncio
async def test_get_evidence_record_returns_503_for_database_failure(install_pool):
    connection, pool = install_pool(error=RuntimeError("database unavailable"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer dev-requester"},
    ) as client:
        response = await client.get(f"/evidence-records/{EVIDENCE_ID}")

    assert response.status_code == 503
    assert response.json() == {"detail": "database query failed"}
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_list_intake_evidence_records_returns_serialized_records(install_pool):
    first_row = evidence_row()
    second_row = evidence_row(
        evidence_id=SECOND_EVIDENCE_ID,
        created_at=datetime(2026, 9, 20, 18, 2, 53, 729753, tzinfo=timezone.utc),
    )
    connection, pool = install_pool(
        row={"intake_id": INTAKE_ID},
        rows=[first_row, second_row],
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer dev-requester"},
    ) as client:
        response = await client.get(
            f"/intake-items/{INTAKE_ID}/evidence-records?limit=2&offset=1"
        )

    assert response.status_code == 200
    assert response.json() == {
        "intake_id": str(INTAKE_ID),
        "limit": 2,
        "offset": 1,
        "items": [
            {
                "evidence_id": str(EVIDENCE_ID),
                "intake_id": str(INTAKE_ID),
                "sha256": first_row["sha256"],
                "storage_reference": first_row["storage_reference"],
                "media_type": first_row["media_type"],
                "filename": first_row["filename"],
                "description": first_row["description"],
                "metadata": {"purpose": "endpoint smoke test", "synthetic": True},
                "correlation_id": str(CORRELATION_ID),
                "created_at": "2026-09-21T18:02:53.729753Z",
            },
            {
                "evidence_id": str(SECOND_EVIDENCE_ID),
                "intake_id": str(INTAKE_ID),
                "sha256": second_row["sha256"],
                "storage_reference": second_row["storage_reference"],
                "media_type": second_row["media_type"],
                "filename": second_row["filename"],
                "description": second_row["description"],
                "metadata": {"purpose": "endpoint smoke test", "synthetic": True},
                "correlation_id": str(CORRELATION_ID),
                "created_at": "2026-09-20T18:02:53.729753Z",
            },
        ],
    }
    assert pool.acquire_count == 1
    assert len(connection.calls) == 2

    intake_method, intake_query, intake_args = connection.calls[0]
    assert intake_method == "fetchrow"
    assert "FROM intake_items" in intake_query
    assert "WHERE intake_id = $1" in intake_query
    assert "AND tenant_id = $2" in intake_query
    assert intake_args == (INTAKE_ID, DEFAULT_TENANT_ID)

    list_method, list_query, list_args = connection.calls[1]
    assert list_method == "fetch"
    assert "FROM evidence_records" in list_query
    assert "WHERE intake_id = $1" in list_query
    assert "AND tenant_id = $2" in list_query
    assert "ORDER BY created_at DESC, evidence_id DESC" in list_query
    assert "LIMIT $3 OFFSET $4" in list_query
    assert list_args == (INTAKE_ID, DEFAULT_TENANT_ID, 2, 1)


@pytest.mark.asyncio
async def test_list_intake_evidence_records_returns_empty_items(install_pool):
    connection, pool = install_pool(row={"intake_id": INTAKE_ID}, rows=[])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer dev-requester"},
    ) as client:
        response = await client.get(f"/intake-items/{INTAKE_ID}/evidence-records")

    assert response.status_code == 200
    assert response.json() == {
        "intake_id": str(INTAKE_ID),
        "limit": 50,
        "offset": 0,
        "items": [],
    }
    assert pool.acquire_count == 1
    assert len(connection.calls) == 2


@pytest.mark.asyncio
async def test_list_intake_evidence_records_returns_404_when_intake_missing(install_pool):
    connection, pool = install_pool(row=None)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer dev-requester"},
    ) as client:
        response = await client.get(
            f"/intake-items/{MISSING_INTAKE_ID}/evidence-records"
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "intake item not found"}
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1
    assert connection.calls[0][0] == "fetchrow"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/intake-items/not-a-uuid/evidence-records",
        f"/intake-items/{INTAKE_ID}/evidence-records?limit=0",
        f"/intake-items/{INTAKE_ID}/evidence-records?limit=101",
        f"/intake-items/{INTAKE_ID}/evidence-records?offset=-1",
    ],
)
async def test_list_intake_evidence_records_rejects_invalid_parameters(install_pool, path):
    connection, pool = install_pool()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer dev-requester"},
    ) as client:
        response = await client.get(path)

    assert response.status_code == 422
    assert connection.calls == []
    assert pool.acquire_count == 0


@pytest.mark.asyncio
async def test_list_intake_evidence_records_returns_503_for_database_failure(install_pool):
    connection, pool = install_pool(error=RuntimeError("database unavailable"))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer dev-requester"},
    ) as client:
        response = await client.get(f"/intake-items/{INTAKE_ID}/evidence-records")

    assert response.status_code == 503
    assert response.json() == {"detail": "database query failed"}
    assert pool.acquire_count == 1
    assert len(connection.calls) == 1


@pytest.mark.asyncio
async def test_evidence_lookup_denies_principal_without_research_permission(
    install_pool,
):
    connection, pool = install_pool()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": "Bearer dev-approver"},
    ) as client:
        response = await client.get(f"/evidence-records/{EVIDENCE_ID}")

    assert response.status_code == 403
    assert response.json() == {"detail": "permission denied"}
    assert connection.calls == []
    assert pool.acquire_count == 0
