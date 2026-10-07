import os
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest

from app import main as main_module
from app.auth import DEFAULT_TENANT_ID
from app.main import app


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_failure_event", [False, True])
async def test_execution_failure_commit_or_rollback(
    monkeypatch, reject_failure_event
):
    database = urlparse(os.environ.get("DATABASE_URL", ""))
    if (
        database.hostname != "postgres-test"
        or database.path != "/atlas_test"
    ):
        pytest.fail("This test requires the isolated postgres-test database.")

    correlation_id = uuid4()
    suffix = correlation_id.hex
    function_name = f"reject_failure_event_{suffix}"
    trigger_name = f"reject_failure_event_trigger_{suffix}"
    headers = {"Authorization": "Bearer dev-admin"}
    trigger_created = False
    function_created = False

    def broken_adapter(*args, **kwargs):
        raise RuntimeError("private integration adapter detail")

    async with app.router.lifespan_context(app):
        pool = app.state.pool
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                created = await client.post(
                    "/jobs",
                    headers=headers,
                    json={
                        "job_type": "metadata.analyze",
                        "request_payload": {
                            "content": "isolated execution failure test"
                        },
                        "approval_required": False,
                        "correlation_id": str(correlation_id),
                    },
                )
                assert created.status_code in (200, 201), created.text
                job_id = created.json()["job_id"]

                async with pool.acquire() as connection:
                    before = await connection.fetchrow(
                        """
                        SELECT status, started_at, completed_at,
                               result_payload, error_code
                        FROM jobs
                        WHERE job_id = $1::uuid AND tenant_id = $2
                        """,
                        job_id,
                        DEFAULT_TENANT_ID,
                    )
                    assert before is not None
                    assert before["status"] == "queued"
                    before_events = await connection.fetchval(
                        """
                        SELECT count(*) FROM events
                        WHERE correlation_id = $1 AND tenant_id = $2
                        """,
                        correlation_id,
                        DEFAULT_TENANT_ID,
                    )

                    if reject_failure_event:
                        await connection.execute(
                            f"""
                            CREATE FUNCTION {function_name}()
                            RETURNS trigger AS $$
                            BEGIN
                              IF NEW.correlation_id = '{correlation_id}'::uuid
                                 AND NEW.event_type = 'jobs.failed' THEN
                                RAISE EXCEPTION 'forced failure-event rejection';
                              END IF;
                              RETURN NEW;
                            END;
                            $$ LANGUAGE plpgsql;
                            """
                        )
                        function_created = True
                        await connection.execute(
                            f"""
                            CREATE TRIGGER {trigger_name}
                            BEFORE INSERT ON events
                            FOR EACH ROW
                            EXECUTE FUNCTION {function_name}();
                            """
                        )
                        trigger_created = True

                monkeypatch.setattr(
                    main_module, "execute_text_analysis", broken_adapter
                )
                response = await client.post(
                    f"/jobs/{job_id}/execute", headers=headers
                )

                async with pool.acquire() as connection:
                    after = await connection.fetchrow(
                        """
                        SELECT status, started_at, completed_at,
                               result_payload, error_code
                        FROM jobs
                        WHERE job_id = $1::uuid AND tenant_id = $2
                        """,
                        job_id,
                        DEFAULT_TENANT_ID,
                    )
                    after_events = await connection.fetchval(
                        """
                        SELECT count(*) FROM events
                        WHERE correlation_id = $1 AND tenant_id = $2
                        """,
                        correlation_id,
                        DEFAULT_TENANT_ID,
                    )
                    execution_events = await connection.fetch(
                        """
                        SELECT event_type FROM events
                        WHERE correlation_id = $1 AND tenant_id = $2
                          AND event_type IN (
                            'jobs.started', 'jobs.failed', 'jobs.succeeded'
                          )
                        """,
                        correlation_id,
                        DEFAULT_TENANT_ID,
                    )

                assert "private integration adapter detail" not in response.text
                if reject_failure_event:
                    assert response.status_code == 503, response.text
                    assert response.json() == {
                        "detail": "database write failed"
                    }
                    assert dict(after) == dict(before)
                    assert after_events == before_events
                    assert execution_events == []
                else:
                    assert response.status_code == 200, response.text
                    assert response.json()["status"] == "failed"
                    assert after["status"] == "failed"
                    assert after["error_code"] == "analysis_execution_failed"
                    assert after["result_payload"] is None
                    assert after["started_at"] is not None
                    assert after["completed_at"] is not None
                    assert after_events == before_events + 2
                    assert sorted(
                        row["event_type"] for row in execution_events
                    ) == ["jobs.failed", "jobs.started"]
        finally:
            async with pool.acquire() as connection:
                if trigger_created:
                    await connection.execute(
                        f"DROP TRIGGER IF EXISTS {trigger_name} ON events"
                    )
                if function_created:
                    await connection.execute(
                        f"DROP FUNCTION IF EXISTS {function_name}()"
                    )
                await connection.execute(
                    """
                    DELETE FROM events
                    WHERE correlation_id = $1 AND tenant_id = $2
                    """,
                    correlation_id,
                    DEFAULT_TENANT_ID,
                )
                await connection.execute(
                    """
                    DELETE FROM jobs
                    WHERE correlation_id = $1 AND tenant_id = $2
                    """,
                    correlation_id,
                    DEFAULT_TENANT_ID,
                )
