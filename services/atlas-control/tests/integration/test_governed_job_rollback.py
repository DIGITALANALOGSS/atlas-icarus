from uuid import uuid4

import httpx
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_failed_governed_job_creation_rolls_back_gate_and_events():
    correlation_id = uuid4()

    async with app.router.lifespan_context(app):
        pool = app.state.pool

        async with pool.acquire() as connection:
            await connection.execute(
                f"""
                CREATE OR REPLACE FUNCTION fail_integration_job_insert()
                RETURNS trigger AS $$
                BEGIN
                  IF NEW.correlation_id = '{correlation_id}'::uuid THEN
                    RAISE EXCEPTION 'forced integration job insert failure';
                  END IF;
                  RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                """
            )
            await connection.execute(
                """
                CREATE TRIGGER fail_integration_job_insert_trigger
                BEFORE INSERT ON jobs
                FOR EACH ROW
                EXECUTE FUNCTION fail_integration_job_insert();
                """
            )

        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/jobs",
                    json={
                        "job_type": "metadata.analyze",
                        "request_payload": {
                            "content": "force a PostgreSQL rollback",
                        },
                        "approval_required": True,
                        "correlation_id": str(correlation_id),
                    },
                )

            assert response.status_code == 503
            assert response.json() == {"detail": "database write failed"}

            async with pool.acquire() as connection:
                approval_gate_count = await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM approval_gates
                    WHERE correlation_id = $1
                    """,
                    correlation_id,
                )
                job_count = await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM jobs
                    WHERE correlation_id = $1
                    """,
                    correlation_id,
                )
                event_count = await connection.fetchval(
                    """
                    SELECT count(*)
                    FROM events
                    WHERE correlation_id = $1
                    """,
                    correlation_id,
                )

            assert approval_gate_count == 0
            assert job_count == 0
            assert event_count == 0
        finally:
            async with pool.acquire() as connection:
                await connection.execute(
                    "DROP TRIGGER IF EXISTS fail_integration_job_insert_trigger ON jobs"
                )
                await connection.execute(
                    "DROP FUNCTION IF EXISTS fail_integration_job_insert()"
                )
