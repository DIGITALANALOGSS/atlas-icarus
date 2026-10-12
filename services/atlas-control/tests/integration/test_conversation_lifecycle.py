import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth import development_principals
from app.conversation_adapters import ConversationModelError
from app.conversation_coordinator import (
    ConversationCoordinatorError, claim_turn, publish_turn, reserve_turn,
)
from app.main import app, write_event


class BoundPool:
    def __init__(self, connection):
        self.connection = connection
        self.in_use = 0

    @asynccontextmanager
    async def acquire(self):
        self.in_use += 1
        try:
            yield self.connection
        finally:
            self.in_use -= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    "success", "model_failure", "cancel", "expiry", "audit_rollback",
])
async def test_conversation_lifecycle_routes(monkeypatch, scenario):
    database = urlparse(os.environ.get("DATABASE_URL", ""))
    if database.hostname != "postgres-test" or database.path != "/atlas_test":
        pytest.fail("Requires the isolated postgres-test database")
    monkeypatch.setenv("ATLAS_CONVERSATION_MODEL", "echo")
    principal = development_principals()["dev-requester"]
    cid, user = uuid4(), uuid4()
    headers = {"Authorization": "Bearer dev-requester"}

    async with app.router.lifespan_context(app):
        real_pool = app.state.pool
        async with real_pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            try:
                await connection.execute(
                    "INSERT INTO conversations "
                    "(conversation_id, tenant_id, owner_subject_id, correlation_id) "
                    "VALUES ($1,$2,$3,$1)",
                    cid, principal.tenant_id, principal.subject_id,
                )
                await connection.execute(
                    "INSERT INTO conversation_messages "
                    "(message_id,tenant_id,conversation_id,sequence_number,role,content) "
                    "VALUES ($1,$2,$3,1,'user','Hello')",
                    user, principal.tenant_id, cid,
                )
                pool = BoundPool(connection)
                monkeypatch.setattr(app.state, "pool", pool)
                calls = []

                class Model:
                    async def complete(self, messages):
                        assert pool.in_use == 0
                        calls.append(messages)
                        assert [(m.role, m.content) for m in messages] == [("user", "Hello")]
                        if scenario == "model_failure":
                            raise ConversationModelError("synthetic-private-provider-detail")
                        return "Reply"

                monkeypatch.setattr(app.state, "conversation_model", Model())
                path = f"/conversations/{cid}/turns"
                body = {"user_message_id": str(user)}

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test",
                ) as client:
                    for auth, expected in [
                        ({}, 401),
                        ({"Authorization": "Bearer dev-worker"}, 403),
                        ({"Authorization": "Bearer dev-admin"}, 404),
                    ]:
                        response = await client.post(path, headers=auth, json=body)
                        assert response.status_code == expected, response.text
                    invalid = await client.post(
                        path, headers=headers, json={**body, "role": "assistant"},
                    )
                    assert invalid.status_code == 422

                    if scenario in {"success", "model_failure"}:
                        response = await client.post(path, headers=headers, json=body)
                        assert response.status_code == (
                            200 if scenario == "success" else 502
                        ), response.text
                        assert "synthetic-private-provider-detail" not in response.text
                        replay = await client.post(path, headers=headers, json=body)
                        assert replay.status_code == (
                            200 if scenario == "success" else 409
                        ), replay.text
                        assert len(calls) == 1
                        if scenario == "success":
                            assert replay.json() == response.json()
                            assert "worker_token" not in response.json()["turn"]
                            assert "lease_expires_at" not in response.json()["turn"]
                            assert response.json()["message"]["role"] == "assistant"
                            assert response.json()["message"]["sequence_number"] == 2
                    else:
                        reserved = await reserve_turn(
                            pool, principal, cid, user, write_event=write_event,
                        )
                        running = await claim_turn(
                            pool, principal, reserved["turn_id"], write_event=write_event,
                        )
                        tid = running["turn_id"]
                        detail = path + f"/{tid}"
                        wrong = await client.post(
                            f"/conversations/{uuid4()}/turns/{tid}/cancel",
                            headers=headers,
                        )
                        assert wrong.status_code == 404
                        foreign = await client.get(
                            detail, headers={"Authorization": "Bearer dev-admin"},
                        )
                        assert foreign.status_code == 404

                        if scenario == "cancel":
                            cancelled = await client.post(detail + "/cancel", headers=headers)
                            assert cancelled.status_code == 200, cancelled.text
                            repeated = await client.post(detail + "/cancel", headers=headers)
                            assert repeated.status_code == 200, repeated.text
                            assert repeated.json() == cancelled.json()
                        elif scenario == "expiry":
                            await connection.execute(
                                "UPDATE conversation_turns SET lease_expires_at = "
                                "clock_timestamp() - INTERVAL '1 second' WHERE turn_id=$1",
                                tid,
                            )
                            expired = await client.get(detail, headers=headers)
                            assert expired.status_code == 200, expired.text
                            assert expired.json()["error_code"] == "lease_expired"
                        else:
                            async def failing_audit(conn, **kwargs):
                                await write_event(conn, **kwargs)
                                raise RuntimeError("injected publication audit failure")

                            with pytest.raises(RuntimeError, match="injected publication"):
                                await publish_turn(
                                    pool, principal, tid,
                                    worker_token=running["worker_token"], content="Reply",
                                    write_event=failing_audit,
                                )
                            assert await connection.fetchval(
                                "SELECT status FROM conversation_turns WHERE turn_id=$1", tid,
                            ) == "running"
                            assert await connection.fetchval(
                                "SELECT count(*) FROM events WHERE correlation_id=$1 "
                                "AND event_type='governance.conversation.turn.completed'", tid,
                            ) == 0

                        if scenario in {"cancel", "expiry"}:
                            with pytest.raises(ConversationCoordinatorError):
                                await publish_turn(
                                    pool, principal, tid,
                                    worker_token=running["worker_token"], content="Late reply",
                                    write_event=write_event,
                                )

                assert await connection.fetchval(
                    "SELECT count(*) FROM conversation_messages "
                    "WHERE conversation_id=$1 AND role='assistant'", cid,
                ) == (1 if scenario == "success" else 0)
                row = await connection.fetchrow(
                    "SELECT * FROM conversation_turns WHERE conversation_id=$1", cid,
                )
                assert row["status"] == {
                    "success": "completed", "model_failure": "failed",
                    "cancel": "cancelled", "expiry": "failed",
                    "audit_rollback": "running",
                }[scenario]
                if scenario == "success":
                    assert row["assistant_message_id"] == UUID(
                        response.json()["message"]["message_id"]
                    )
                if scenario == "cancel":
                    assert await connection.fetchval(
                        "SELECT count(*) FROM events WHERE correlation_id=$1 "
                        "AND event_type='governance.conversation.turn.cancelled'",
                        row["correlation_id"],
                    ) == 1
            finally:
                await transaction.rollback()
                app.state.pool = real_pool
