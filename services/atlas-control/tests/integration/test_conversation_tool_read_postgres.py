import json
import os
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from uuid import UUID, uuid4
import httpx
import pytest

from app.auth import Principal, development_principals
from app.conversation_coordinator import ConversationCoordinatorError
from app.conversation_tool_calls import (
    ModelToolCall, StructuredModelReply, ToolCapableConversationModel,
)
from app.conversation_tool_read import read_tool_job
from app.main import app
from test_conversation_lifecycle import BoundPool


@pytest.mark.asyncio
async def test_lookup_result_replay_and_scope(monkeypatch):
    database = urlparse(os.environ.get("DATABASE_URL", ""))
    if database.hostname != "postgres-test" or database.path != "/atlas_test":
        pytest.fail("Requires isolated postgres-test/atlas_test")

    required = {
        "conversations:read", "conversations:write", "jobs:create", "jobs:read",
    }
    eligible = [
        (token, actor)
        for token, actor in development_principals().items()
        if required.issubset(actor.permissions)
    ]
    assert eligible, "Required development requester unavailable"
    token, actor = eligible[0]

    monkeypatch.setenv("ATLAS_CONVERSATION_TOOL_PROPOSALS", "1")
    monkeypatch.setenv("ATLAS_CONVERSATION_MODEL", "llama_cpp")
    monkeypatch.setenv("ATLAS_MODEL_API_KEY", "synthetic-lookup-test-key")
    monkeypatch.setenv("ATLAS_MODEL_BASE_URL", "http://model.invalid/v1")
    monkeypatch.setenv("ATLAS_MODEL_TIMEOUT_SECONDS", "30")

    create_reply = StructuredModelReply("", (
        ModelToolCall(
            "create-1", "atlas_metadata_analyze",
            '{"content":"Isolated result lookup fixture"}',
        ),
    ), "tool_calls")
    cid, user = uuid4(), uuid4()

    with patch.object(
        ToolCapableConversationModel, "complete_with_tools",
        new_callable=AsyncMock, return_value=create_reply,
    ) as generate:
        async with app.router.lifespan_context(app):
            real_pool = app.state.pool
            async with real_pool.acquire() as connection:
                outer = connection.transaction()
                await outer.start()
                try:
                    await connection.execute(
                        "INSERT INTO conversations "
                        "(conversation_id,tenant_id,owner_subject_id,correlation_id) "
                        "VALUES ($1,$2,$3,$1)",
                        cid, actor.tenant_id, actor.subject_id,
                    )
                    await connection.execute(
                        "INSERT INTO conversation_messages "
                        "(message_id,tenant_id,conversation_id,sequence_number,role,content) "
                        "VALUES ($1,$2,$3,1,'user','Prepare analysis')",
                        user, actor.tenant_id, cid,
                    )
                    app.state.pool = BoundPool(connection)
                    headers = {"Authorization": "Bearer " + token}
                    path = f"/conversations/{cid}/turns"

                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://test",
                    ) as client:
                        first = await client.post(
                            path, headers=headers,
                            json={"user_message_id": str(user)},
                        )
                        assert first.status_code == 200, first.text
                        correlation = UUID(first.json()["turn"]["correlation_id"])
                        job_id = await connection.fetchval(
                            "SELECT job_id FROM jobs "
                            "WHERE tenant_id=$1 AND correlation_id=$2",
                            actor.tenant_id, correlation,
                        )
                        lookup = ModelToolCall(
                            "read-1", "atlas_job_status",
                            json.dumps({"job_id": str(job_id)}),
                        )
                        pending = await read_tool_job(
                            connection, actor, {"conversation_id": cid}, lookup,
                        )
                        assert '"status": "pending_approval"' in pending

                        # Test fixture only; not a live execution or approval.
                        await connection.execute(
                            "UPDATE jobs SET status='succeeded', "
                            "result_payload=$2::jsonb, completed_at=clock_timestamp() "
                            "WHERE job_id=$1",
                            job_id, json.dumps({"fixture_result": 7}),
                        )

                        for scoped_actor, scoped_cid in (
                            (actor, uuid4()),
                            (Principal(
                                "other-owner", actor.tenant_id,
                                actor.permissions, "user",
                            ), cid),
                            (Principal(
                                actor.subject_id, uuid4(),
                                actor.permissions, "user",
                            ), cid),
                        ):
                            with pytest.raises(
                                ConversationCoordinatorError,
                                match="job unavailable",
                            ):
                                await read_tool_job(
                                    connection, scoped_actor,
                                    {"conversation_id": scoped_cid}, lookup,
                                )

                        next_user = uuid4()
                        await connection.execute(
                            "INSERT INTO conversation_messages "
                            "(message_id,tenant_id,conversation_id,sequence_number,role,content) "
                            "VALUES ($1,$2,$3,3,'user','Read the saved result')",
                            next_user, actor.tenant_id, cid,
                        )
                        generate.return_value = StructuredModelReply(
                            "", (lookup,), "tool_calls",
                        )
                        body = {"user_message_id": str(next_user)}
                        result = await client.post(path, headers=headers, json=body)
                        assert result.status_code == 200, result.text
                        content = result.json()["message"]["content"]
                        assert '"status": "succeeded"' in content
                        assert '"fixture_result": 7' in content

                        replay = await client.post(path, headers=headers, json=body)
                        assert replay.status_code == 200, replay.text
                        assert replay.json() == result.json()
                        assert generate.await_count == 2

                    assert await connection.fetchval(
                        "SELECT count(*) FROM jobs "
                        "WHERE tenant_id=$1 AND correlation_id=$2",
                        actor.tenant_id, correlation,
                    ) == 1
                    assert await connection.fetchval(
                        "SELECT count(*) FROM approval_gates "
                        "WHERE tenant_id=$1 AND correlation_id=$2",
                        actor.tenant_id, correlation,
                    ) == 1
                finally:
                    app.state.pool = real_pool
                    await outer.rollback()
