import hashlib
import os
from urllib.parse import urlparse
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from app.auth import CROSS_TENANT_ID, DEFAULT_TENANT_ID
from app.main import app


@pytest.mark.asyncio
async def test_linked_job_integrity_and_database_tenant_constraint():
    database = urlparse(os.environ.get("DATABASE_URL", ""))
    if database.hostname != "postgres-test" or database.path != "/atlas_test":
        pytest.fail("Requires the isolated postgres-test database.")

    correlation = uuid4()
    content = "isolated linked evidence"
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    headers = {"Authorization": "Bearer dev-admin"}

    async with app.router.lifespan_context(app):
        pool = app.state.pool
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                intake = await client.post(
                    "/intake-items", headers=headers,
                    json={
                        "source": "synthetic integration",
                        "received_at": "2026-10-08T12:00:00Z",
                        "custodian": "integration test",
                        "storage_reference": "synthetic://linked/original",
                        "original_sha256": digest,
                        "correlation_id": str(correlation),
                    },
                )
                assert intake.status_code == 201, intake.text
                intake_id = intake.json()["intake_id"]
                evidence = await client.post(
                    f"/intake-items/{intake_id}/evidence-records",
                    headers=headers,
                    json={
                        "sha256": digest,
                        "storage_reference": "synthetic://linked/working-copy",
                    },
                )
                assert evidence.status_code == 201, evidence.text
                evidence_id = evidence.json()["evidence_id"]

                payload = {
                    "job_type": "metadata.analyze",
                    "request_payload": {"content": content},
                    "evidence_id": evidence_id,
                    "approval_required": False,
                }
                bad = await client.post(
                    "/jobs", headers=headers,
                    json={**payload, "request_payload": {"content": "different"}},
                )
                assert bad.status_code == 409, bad.text
                unavailable = await client.post(
                    "/jobs", headers=headers,
                    json={**payload, "evidence_id": str(uuid4())},
                )
                assert unavailable.status_code == 404, unavailable.text
                assert unavailable.json() == {"detail": "evidence record not found"}

                foreign_evidence_id = uuid4()
                async with pool.acquire() as connection:
                    async with connection.transaction():
                        await connection.execute(
                            """
                            INSERT INTO tenants (tenant_id, slug)
                            VALUES ($1, $2)
                            ON CONFLICT (tenant_id) DO NOTHING
                            """,
                            CROSS_TENANT_ID, "linked-test-other",
                        )
                        await connection.execute(
                            """
                            INSERT INTO intake_items (
                                intake_id, tenant_id, source, received_at,
                                custodian, storage_reference, original_sha256,
                                correlation_id
                            )
                            VALUES ($1, $2, $3, NOW(), $3, $3, $4, $5)
                            """,
                            foreign_evidence_id, CROSS_TENANT_ID,
                            "synthetic foreign evidence", digest, correlation,
                        )
                        await connection.execute(
                            """
                            INSERT INTO evidence_records (
                                evidence_id, tenant_id, intake_id, sha256,
                                storage_reference, correlation_id
                            )
                            VALUES ($1, $2, $1, $3, $4, $5)
                            """,
                            foreign_evidence_id, CROSS_TENANT_ID, digest,
                            "synthetic://foreign/working-copy", correlation,
                        )
                    try:
                        foreign = await client.post(
                            "/jobs", headers=headers,
                            json={
                                **payload,
                                "evidence_id": str(foreign_evidence_id),
                                "approval_required": True,
                            },
                        )
                        assert foreign.status_code == 404, foreign.text
                        assert foreign.json() == unavailable.json()
                        assert await connection.fetchval(
                            """
                            SELECT count(*) FROM jobs
                            WHERE tenant_id = $1 AND correlation_id = $2
                            """,
                            DEFAULT_TENANT_ID, correlation,
                        ) == 0
                        assert await connection.fetchval(
                            """
                            SELECT count(*) FROM approval_gates
                            WHERE tenant_id = $1 AND correlation_id = $2
                            """,
                            DEFAULT_TENANT_ID, correlation,
                        ) == 0
                    finally:
                        async with connection.transaction():
                            await connection.execute(
                                "DELETE FROM evidence_records WHERE evidence_id = $1 AND tenant_id = $2",
                                foreign_evidence_id, CROSS_TENANT_ID,
                            )
                            await connection.execute(
                                "DELETE FROM intake_items WHERE intake_id = $1 AND tenant_id = $2",
                                foreign_evidence_id, CROSS_TENANT_ID,
                            )

                created = await client.post("/jobs", headers=headers, json=payload)
                assert created.status_code == 201, created.text
                job_id = created.json()["job_id"]
                assert created.json()["correlation_id"] == str(correlation)
                read = await client.get(f"/jobs/{job_id}", headers=headers)
                assert read.json()["evidence_id"] == evidence_id
                executed = await client.post(
                    f"/jobs/{job_id}/execute", headers=headers
                )
                assert executed.status_code == 200, executed.text
                assert executed.json()["status"] == "succeeded"
                assert executed.json()["evidence_id"] == evidence_id

            async with pool.acquire() as connection:
                assert await connection.fetchval(
                    "SELECT evidence_id FROM jobs WHERE job_id = $1",
                    UUID(job_id),
                ) == UUID(evidence_id)
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO tenants (tenant_id, slug)
                        VALUES ($1, 'linked-test-other')
                        ON CONFLICT (tenant_id) DO NOTHING
                        """,
                        CROSS_TENANT_ID,
                    )
                    with pytest.raises(asyncpg.ForeignKeyViolationError):
                        async with connection.transaction():
                            await connection.execute(
                                """
                                INSERT INTO jobs (
                                  job_id, tenant_id, job_type, request_payload,
                                  status, approval_required, correlation_id,
                                  evidence_id
                                )
                                VALUES ($1, $2, 'metadata.analyze', $3::jsonb,
                                        'queued', false, $4, $5)
                                """,
                                uuid4(), CROSS_TENANT_ID,
                                '{"content":"isolated linked evidence"}',
                                correlation, UUID(evidence_id),
                            )
        finally:
            async with pool.acquire() as connection:
                async with connection.transaction():
                    for table in ("jobs", "evidence_records", "intake_items", "events"):
                        await connection.execute(
                            f"DELETE FROM {table} WHERE correlation_id = $1 AND tenant_id = $2",
                            correlation, DEFAULT_TENANT_ID,
                        )
