import os
from urllib.parse import urlparse
from uuid import uuid4

import asyncpg
import pytest

from app.auth import development_principals
from app.main import app, write_event
from app.conversation_coordinator import ConversationCoordinatorError, claim_turn
from contextlib import asynccontextmanager


class BoundConnectionPool:
    def __init__(self, connection):
        self.connection = connection

    @asynccontextmanager
    async def acquire(self):
        yield self.connection


@pytest.mark.asyncio
async def test_conversation_turn_database_constraints():
    database = urlparse(os.environ.get("DATABASE_URL", ""))
    if database.hostname != "postgres-test" or database.path != "/atlas_test":
        pytest.fail("Requires the isolated postgres-test database.")
    principal = development_principals()["dev-requester"]
    tenant = principal.tenant_id
    cid, other_cid, turn, user, assistant, second_user = [uuid4() for _ in range(6)]
    async with app.router.lifespan_context(app):
        async with app.state.pool.acquire() as connection:
            transaction = connection.transaction()
            await transaction.start()
            try:
                await connection.executemany(
                    "INSERT INTO conversations (conversation_id, tenant_id, owner_subject_id, correlation_id) VALUES ($1,$2,$3,$1)",
                    [(cid, tenant, principal.subject_id), (other_cid, tenant, principal.subject_id)],
                )
                await connection.executemany(
                    "INSERT INTO conversation_messages (message_id,tenant_id,conversation_id,sequence_number,role,content) VALUES ($1,$2,$3,$4,$5,$6)",
                    [
                        (user, tenant, cid, 1, "user", "First"),
                        (assistant, tenant, cid, 2, "assistant", "Reply"),
                        (second_user, tenant, cid, 3, "user", "Next"),
                    ],
                )
                insert = (
                    "INSERT INTO conversation_turns "
                    "(turn_id,tenant_id,conversation_id,user_message_id,correlation_id,"
                    "context_sequence_number,context_sha256,status) "
                    "VALUES ($1,$2,$3,$4,$1,1,$5,$6)"
                )
                await connection.execute(insert, turn, tenant, cid, user, "0" * 64, "queued")

                for update in (
                    "SET status = 'running'",
                    "SET status = 'completed', completed_at = NOW()",
                    "SET status = 'failed', completed_at = NOW()",
                    "SET status = 'cancelled'",
                ):
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with connection.transaction():
                            await connection.execute(
                                "UPDATE conversation_turns " + update + " WHERE turn_id = $1", turn,
                            )

                with pytest.raises(asyncpg.UniqueViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            insert, uuid4(), tenant, cid, second_user, "0" * 64, "queued",
                        )
                with pytest.raises(asyncpg.ForeignKeyViolationError):
                    async with connection.transaction():
                        await connection.execute(
                            insert, uuid4(), tenant, other_cid, user, "0" * 64, "queued",
                        )
                pool = BoundConnectionPool(connection)

                async def failing_audit(conn, **kwargs):
                    await write_event(conn, **kwargs)
                    raise RuntimeError("injected audit failure")

                with pytest.raises(RuntimeError, match="injected audit failure"):
                    await claim_turn(
                        pool, principal, turn, write_event=failing_audit,
                        lease_seconds=60,
                    )
                rolled_back = await connection.fetchrow(
                    "SELECT status, attempt, worker_token, started_at, lease_expires_at "
                    "FROM conversation_turns WHERE turn_id = $1", turn,
                )
                assert dict(rolled_back) == {
                    "status": "queued", "attempt": 0, "worker_token": None,
                    "started_at": None, "lease_expires_at": None,
                }
                event_count_sql = (
                    "SELECT count(*) FROM events WHERE tenant_id = $1 "
                    "AND correlation_id = $2 "
                    "AND event_type = 'governance.conversation.turn.running'"
                )
                assert await connection.fetchval(event_count_sql, tenant, turn) == 0

                claimed = await claim_turn(
                    pool, principal, turn, write_event=write_event,
                    lease_seconds=60,
                )
                assert claimed["status"] == "running"
                assert claimed["attempt"] == 1
                assert claimed["worker_token"] is not None
                assert claimed["lease_expires_at"] > claimed["started_at"]
                persisted = await connection.fetchrow(
                    "SELECT * FROM conversation_turns WHERE turn_id = $1", turn,
                )
                assert dict(persisted) == claimed
                assert await connection.fetchval(event_count_sql, tenant, turn) == 1

                with pytest.raises(ConversationCoordinatorError, match="turn unavailable"):
                    await claim_turn(
                        pool, principal, turn, write_event=write_event,
                        lease_seconds=60,
                    )
                assert await connection.fetchval(event_count_sql, tenant, turn) == 1
                assert await connection.fetchval(
                    "SELECT worker_token FROM conversation_turns WHERE turn_id = $1",
                    turn,
                ) == claimed["worker_token"]
                await connection.execute(
                    "UPDATE conversation_turns SET status = 'completed', "
                    "assistant_message_id = $2, completed_at = NOW(), "
                    "worker_token = NULL, lease_expires_at = NULL WHERE turn_id = $1",
                    turn, assistant,
                )
                await connection.execute(
                    insert, uuid4(), tenant, cid, second_user, "0" * 64, "queued",
                )
                assert await connection.fetchval(
                    "SELECT count(*) FROM conversation_turns WHERE conversation_id = $1", cid,
                ) == 2
            finally:
                await transaction.rollback()
