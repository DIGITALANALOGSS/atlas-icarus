import json
import os
from unittest.mock import AsyncMock, patch
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth import development_principals
from app.conversation_tool_calls import (
    ModelToolCall,
    StructuredModelReply,
    ToolCapableConversationModel,
)
from app.main import app
from test_conversation_lifecycle import BoundPool


@pytest.mark.asyncio
async def test_http_tool_proposal_creates_pending_job_and_replays(
    monkeypatch,
):
    database = urlparse(os.environ.get("DATABASE_URL", ""))
    if (
        database.hostname != "postgres-test"
        or database.path != "/atlas_test"
    ):
        pytest.fail("Requires isolated postgres-test/atlas_test")

    required = {
        "conversations:read",
        "conversations:write",
        "jobs:create",
    }
    eligible = [
        (token, principal)
        for token, principal in development_principals().items()
        if required.issubset(principal.permissions)
    ]
    assert eligible, (
        "No development principal has required proposal permissions"
    )
    token, principal = eligible[0]

    monkeypatch.setenv("ATLAS_CONVERSATION_TOOL_PROPOSALS", "1")
    monkeypatch.setenv("ATLAS_CONVERSATION_MODEL", "llama_cpp")
    monkeypatch.setenv(
        "ATLAS_MODEL_API_KEY", "synthetic-http-test-key"
    )
    monkeypatch.setenv(
        "ATLAS_MODEL_BASE_URL", "http://model.invalid/v1"
    )
    monkeypatch.setenv("ATLAS_MODEL_TIMEOUT_SECONDS", "30")

    proposal = ModelToolCall(
        "http-call-1",
        "atlas_metadata_analyze",
        '{"content":"HTTP proposal qualification"}',
    )
    response = StructuredModelReply(
        "", (proposal,), "tool_calls"
    )
    cid, user = uuid4(), uuid4()

    with patch.object(
        ToolCapableConversationModel,
        "complete_with_tools",
        new_callable=AsyncMock,
        return_value=response,
    ) as generate:
        async with app.router.lifespan_context(app):
            real_pool = app.state.pool
            assert isinstance(
                app.state.conversation_model,
                ToolCapableConversationModel,
            )
            async with real_pool.acquire() as connection:
                outer = connection.transaction()
                await outer.start()
                try:
                    await connection.execute(
                        "INSERT INTO conversations "
                        "(conversation_id,tenant_id,"
                        "owner_subject_id,correlation_id) "
                        "VALUES ($1,$2,$3,$1)",
                        cid,
                        principal.tenant_id,
                        principal.subject_id,
                    )
                    await connection.execute(
                        "INSERT INTO conversation_messages "
                        "(message_id,tenant_id,conversation_id,"
                        "sequence_number,role,content) "
                        "VALUES ($1,$2,$3,1,'user',"
                        "'Prepare analysis for approval')",
                        user,
                        principal.tenant_id,
                        cid,
                    )
                    app.state.pool = BoundPool(connection)

                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://test",
                    ) as client:
                        path = f"/conversations/{cid}/turns"
                        headers = {
                            "Authorization": "Bearer " + token
                        }
                        body = {"user_message_id": str(user)}
                        first = await client.post(
                            path, headers=headers, json=body
                        )
                        assert first.status_code == 200, first.text
                        replay = await client.post(
                            path, headers=headers, json=body
                        )
                        assert replay.status_code == 200, replay.text
                        assert replay.json() == first.json()

                    generate.assert_awaited_once()
                    assert (
                        "pending approval"
                        in first.json()["message"]["content"]
                    )
                    correlation = UUID(
                        first.json()["turn"]["correlation_id"]
                    )
                    jobs = await connection.fetch(
                        "SELECT * FROM jobs "
                        "WHERE tenant_id=$1 AND correlation_id=$2",
                        principal.tenant_id,
                        correlation,
                    )
                    gates = await connection.fetch(
                        "SELECT * FROM approval_gates "
                        "WHERE tenant_id=$1 AND correlation_id=$2",
                        principal.tenant_id,
                        correlation,
                    )
                    assert len(jobs) == len(gates) == 1
                    assert jobs[0]["status"] == "pending_approval"
                    assert gates[0]["status"] == "pending"
                    assert (
                        jobs[0]["approval_gate_id"]
                        == gates[0]["gate_id"]
                    )
                    payload = jobs[0]["request_payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    assert (
                        payload["content"]
                        == "HTTP proposal qualification"
                    )
                    assert (
                        payload["_atlas_proposal"]["tool_call_id"]
                        == proposal.id
                    )
                    assert (
                        payload["_atlas_proposal"]["arguments_json"]
                        == proposal.arguments_json
                    )
                finally:
                    app.state.pool = real_pool
                    await outer.rollback()
