from pathlib import Path
from uuid import UUID
import sys

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from app.auth import (
    DEFAULT_TENANT_ID,
    Principal,
    development_principals,
    get_principal,
    require_permission,
)


@pytest.fixture(autouse=True)
def clear_development_principal_cache(monkeypatch):
    for name in (
        "ATLAS_DEV_TENANT_ID",
        "ATLAS_DEV_REQUESTER_ID",
        "ATLAS_DEV_APPROVER_ID",
        "ATLAS_DEV_WORKER_ID",
        "ATLAS_DEV_ADMIN_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    development_principals.cache_clear()
    yield
    development_principals.cache_clear()


def auth_test_app() -> FastAPI:
    app = FastAPI()

    @app.get("/create")
    async def create_route(
        principal: Principal = Depends(require_permission("approval-gates:create")),
    ) -> dict:
        return {
            "subject_id": principal.subject_id,
            "tenant_id": str(principal.tenant_id),
            "principal_type": principal.principal_type,
        }

    @app.get("/decide")
    async def decide_route(
        principal: Principal = Depends(require_permission("approval-gates:decide")),
    ) -> dict:
        return {"subject_id": principal.subject_id}

    @app.get("/execute")
    async def execute_route(
        principal: Principal = Depends(require_permission("jobs:execute")),
    ) -> dict:
        return {
            "subject_id": principal.subject_id,
            "principal_type": principal.principal_type,
        }

    return app


@pytest.mark.asyncio
async def test_missing_bearer_credential_returns_401():
    transport = ASGITransport(app=auth_test_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/create")

    assert response.status_code == 401
    assert response.json() == {"detail": "missing authorization credential"}
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    ["", "Basic dev-requester", "Bearer", "Bearer   "],
)
async def test_malformed_bearer_credential_returns_401(authorization):
    transport = ASGITransport(app=auth_test_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/create",
            headers={"Authorization": authorization},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid authorization credential"}
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_unknown_bearer_credential_returns_401():
    transport = ASGITransport(app=auth_test_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/create",
            headers={"Authorization": "Bearer unknown-token"},
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "invalid authorization credential"}


@pytest.mark.asyncio
async def test_requester_has_create_permission_and_server_derived_identity():
    transport = ASGITransport(app=auth_test_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/create",
            headers={"Authorization": "Bearer dev-requester"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "subject_id": "atlas-requester",
        "tenant_id": str(DEFAULT_TENANT_ID),
        "principal_type": "user",
    }


@pytest.mark.asyncio
async def test_requester_cannot_decide():
    transport = ASGITransport(app=auth_test_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/decide",
            headers={"Authorization": "Bearer dev-requester"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "permission denied"}


@pytest.mark.asyncio
async def test_approver_can_decide():
    transport = ASGITransport(app=auth_test_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/decide",
            headers={"Authorization": "Bearer dev-approver"},
        )

    assert response.status_code == 200
    assert response.json() == {"subject_id": "atlas-approver"}


@pytest.mark.asyncio
async def test_worker_can_execute_as_service_identity():
    transport = ASGITransport(app=auth_test_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/execute",
            headers={"Authorization": "Bearer dev-worker"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "subject_id": "atlas-job-worker",
        "principal_type": "service",
    }


@pytest.mark.asyncio
async def test_approver_cannot_execute():
    transport = ASGITransport(app=auth_test_app())

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/execute",
            headers={"Authorization": "Bearer dev-approver"},
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "permission denied"}


@pytest.mark.asyncio
async def test_development_identity_configuration_changes_principal(monkeypatch):
    monkeypatch.setenv("ATLAS_DEV_TENANT_ID", "22222222-2222-2222-2222-222222222222")
    monkeypatch.setenv("ATLAS_DEV_WORKER_ID", "local-worker")
    development_principals.cache_clear()

    principal = await get_principal("Bearer dev-worker")

    assert principal == Principal(
        subject_id="local-worker",
        tenant_id=UUID("22222222-2222-2222-2222-222222222222"),
        permissions=frozenset({"jobs:execute", "jobs:read"}),
        principal_type="service",
    )
