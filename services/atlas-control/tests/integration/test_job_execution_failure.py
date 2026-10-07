import json
import os
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest

from app import main as main_module
from app.auth import DEFAULT_TENANT_ID
from app.main import app
from app.workflows import WorkflowNodeResult, WorkflowStatus


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "adapter_failure,reject_failure_event",
    [(False, False), (True, False), (True, True)],
)
async def test_execution_failure_commit_or_rollback(
    monkeypatch, adapter_failure, reject_failure_event
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
                               result_payload, error_code,
                               workflow_result
                        FROM jobs
                        WHERE job_id = $1::uuid AND tenant_id = $2
                        """,
                        job_id,
                        DEFAULT_TENANT_ID,
                    )
                    assert before is not None
                    assert before["status"] == "queued"
                    assert before["workflow_result"] is None
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

                if adapter_failure:
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
                               result_payload, error_code,
                               workflow_result
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

                own_read = await client.get(
                    f"/jobs/{job_id}/workflow-result",
                    headers=headers,
                )
                other_read = await client.get(
                    f"/jobs/{job_id}/workflow-result",
                    headers={
                        "Authorization": "Bearer dev-cross-tenant-operator"
                    },
                )
                assert other_read.status_code == 404, other_read.text
                assert other_read.json() == {"detail": "job not found"}

                if reject_failure_event:
                    assert own_read.status_code == 409, own_read.text
                    assert own_read.json() == {
                        "detail": "workflow result is not available"
                    }
                else:
                    assert own_read.status_code == 200, own_read.text
                    stored = after["workflow_result"]
                    if isinstance(stored, str):
                        stored = json.loads(stored)
                    expected_result = WorkflowNodeResult.model_validate(stored)
                    read_result = WorkflowNodeResult.model_validate(
                        own_read.json()
                    )
                    assert read_result == expected_result

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
                    body = response.json()
                    expected = "failed" if adapter_failure else "succeeded"
                    assert body["status"] == expected
                    assert after["status"] == expected
                    assert after["started_at"] is not None
                    assert after["completed_at"] is not None
                    assert after_events == before_events + 2
                    assert sorted(
                        row["event_type"] for row in execution_events
                    ) == sorted(["jobs.started", f"jobs.{expected}"])
                    assert "workflow_result" not in body

                    raw = after["workflow_result"]
                    assert raw is not None
                    data = json.loads(raw) if isinstance(raw, str) else raw
                    envelope = WorkflowNodeResult.model_validate(data)
                    assert envelope.node_id == UUID(job_id)
                    assert envelope.workflow_id == UUID(job_id)
                    assert envelope.tenant_id == DEFAULT_TENANT_ID
                    assert envelope.correlation_id == correlation_id
                    assert envelope.completed_at == after["completed_at"]
                    assert "private integration adapter detail" not in str(raw)

                    if adapter_failure:
                        assert envelope.status == WorkflowStatus.FAILED
                        assert envelope.error is not None
                        assert envelope.error.code == "analysis_execution_failed"
                        assert envelope.output == {}
                        assert after["error_code"] == envelope.error.code
                        assert after["result_payload"] is None
                    else:
                        assert envelope.status == WorkflowStatus.SUCCEEDED
                        assert envelope.error is None
                        assert after["error_code"] is None
                        stored_output = after["result_payload"]
                        if isinstance(stored_output, str):
                            stored_output = json.loads(stored_output)
                        assert envelope.output == stored_output
                        assert envelope.output == body["result_payload"]
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
