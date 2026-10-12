import asyncio
import os
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth import development_principals
from app.main import app


@pytest.mark.asyncio
async def test_conversation_persistence_order_replay_and_owner_isolation():
    database = urlparse(os.environ.get("DATABASE_URL", ""))
    if database.hostname != "postgres-test" or database.path != "/atlas_test":
        pytest.fail("Requires the isolated postgres-test database.")

    principal = development_principals()["dev-requester"]
    headers = {"Authorization": "Bearer dev-requester"}
    cid = None
    async with app.router.lifespan_context(app):
        pool = app.state.pool
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                created = await client.post(
                    "/conversations", headers=headers,
                    json={"title": "Synthetic integration conversation"},
                )
                assert created.status_code == 201, created.text
                cid = UUID(created.json()["conversation_id"])
                path = f"/conversations/{cid}"
                foreign = await client.get(
                    path, headers={"Authorization": "Bearer dev-admin"},
                )
                assert foreign.status_code == 404
                foreign_write = await client.post(
                    path + "/messages",
                    headers={"Authorization": "Bearer dev-admin"},
                    json={"content": "Unauthorized owner write"},
                )
                assert foreign_write.status_code == 404

                payload = {"content": "First", "client_message_id": str(uuid4())}
                first = await client.post(path + "/messages", headers=headers, json=payload)
                assert first.status_code == 201, first.text
                replay = await client.post(path + "/messages", headers=headers, json=payload)
                assert replay.status_code == 201, replay.text
                assert replay.json() == first.json()
                conflict = await client.post(
                    path + "/messages", headers=headers,
                    json={**payload, "content": "Different"},
                )
                assert conflict.status_code == 409

                replies = await asyncio.gather(*[
                    client.post(
                        path + "/messages", headers=headers,
                        json={"content": text, "client_message_id": str(uuid4())},
                    )
                    for text in ("Second", "Third")
                ])
                assert all(reply.status_code == 201 for reply in replies)
                listed = await client.get(path + "/messages", headers=headers)
                assert listed.status_code == 200, listed.text
                items = listed.json()["items"]
                assert [item["sequence_number"] for item in items] == [1, 2, 3]
                assert items[0]["content"] == "First"
                assert {item["content"] for item in items[1:]} == {"Second", "Third"}
                assert all(item["role"] == "user" for item in items)

            async with pool.acquire() as connection:
                assert await connection.fetchval(
                    "SELECT count(*) FROM conversation_messages WHERE conversation_id = $1 AND tenant_id = $2",
                    cid, principal.tenant_id,
                ) == 3
        finally:
            if cid is not None:
                async with pool.acquire() as connection:
                    async with connection.transaction():
                        await connection.execute(
                            "DELETE FROM conversation_messages WHERE conversation_id = $1 AND tenant_id = $2",
                            cid, principal.tenant_id,
                        )
                        await connection.execute(
                            "DELETE FROM conversations WHERE conversation_id = $1 AND tenant_id = $2 AND owner_subject_id = $3",
                            cid, principal.tenant_id, principal.subject_id,
                        )
