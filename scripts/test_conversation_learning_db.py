"""Rollback-only learning-worker checks using the running container."""
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
prefix = "import sys, types\n"
for name in ("conversation_learning", "conversation_learning_worker"):
    path = root / "services/atlas-control/app" / (name + ".py")
    text = path.read_text(encoding="utf-8")
    compile(text, str(path), "exec")
    prefix += (
        f"m = types.ModuleType('app.{name}')\n"
        f"sys.modules['app.{name}'] = m\n"
        f"exec({text!r}, m.__dict__)\n"
    )

checks = r"""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4
import asyncpg
from app.conversation_learning_worker import process_one

TEXT = "I prefer spoken explanations before terminal commands."

class Model:
    def __init__(self, raw):
        self.raw = raw
        self.calls = 0

    async def complete(self, messages):
        self.calls += 1
        return self.raw

async def main():
    c = await asyncpg.connect(os.environ["DATABASE_URL"], command_timeout=10)
    tx = c.transaction()
    await tx.start()
    try:
        await c.execute("SET LOCAL lock_timeout = '3s'")
        tenant = await c.fetchval("SELECT tenant_id FROM tenants LIMIT 1")
        if tenant is None:
            raise RuntimeError("No existing tenant available for test fixtures")
        owner = "learning-test-" + str(uuid4())
        principal = SimpleNamespace(
            tenant_id=tenant, subject_id=owner,
            permissions={"conversations:read", "conversations:write"},
        )
        cid = uuid4()
        await c.execute(
            "INSERT INTO conversations "
            "(conversation_id,tenant_id,owner_subject_id,correlation_id) "
            "VALUES ($1,$2,$3,$4)", cid, tenant, owner, uuid4()
        )

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield c

        pool = Pool()
        model = Model(json.dumps({"memories": [{
            "category": "communication_preference", "quote": TEXT,
        }]}))
        assert await process_one(pool, principal, model=model) is None
        assert model.calls == 0
        print("PASS: empty queue makes no model call")

        denied = SimpleNamespace(
            tenant_id=tenant, subject_id=owner, permissions=set()
        )
        try:
            await process_one(pool, denied, model=model)
        except PermissionError:
            pass
        else:
            raise AssertionError("Missing permissions were accepted")
        print("PASS: missing permissions rejected")

        async def enqueue(sequence):
            mid, jid = uuid4(), uuid4()
            await c.execute(
                "INSERT INTO conversation_messages "
                "(message_id,tenant_id,conversation_id,sequence_number,role,content) "
                "VALUES ($1,$2,$3,$4,'user',$5)",
                mid, tenant, cid, sequence, TEXT,
            )
            await c.execute(
                "INSERT INTO conversation_learning_jobs "
                "(job_id,tenant_id,conversation_id,source_message_id) "
                "VALUES ($1,$2,$3,$4)", jid, tenant, cid, mid,
            )
            return jid

        jid = await enqueue(1)
        other = SimpleNamespace(
            tenant_id=tenant, subject_id=owner + "-other",
            permissions=principal.permissions,
        )
        assert await process_one(pool, other, model=model) is None
        other.tenant_id, other.subject_id = uuid4(), owner
        assert await process_one(pool, other, model=model) is None
        assert model.calls == 0
        print("PASS: other owner and tenant cannot claim the job")

        result = await process_one(pool, principal, model=model)
        assert result == {"job_id": str(jid), "proposals": 1}
        row = await c.fetchrow(
            "SELECT status,content FROM conversation_memory_proposals "
            "WHERE job_id=$1", jid,
        )
        assert dict(row) == {"status": "proposed", "content": TEXT}
        row = await c.fetchrow(
            "SELECT status,worker_token,lease_expires_at "
            "FROM conversation_learning_jobs WHERE job_id=$1", jid,
        )
        assert dict(row) == {
            "status": "completed", "worker_token": None, "lease_expires_at": None,
        }
        print("PASS: successful extraction stores an unapproved proposal")

        assert await process_one(pool, principal, model=model) is None
        assert model.calls == 1
        print("PASS: completed job is not processed again")

        bad_job = await enqueue(2)
        bad_model = Model("not JSON")
        for attempt in range(1, 4):
            try:
                await process_one(pool, principal, model=bad_model)
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid model output was accepted")
            row = await c.fetchrow(
                "SELECT status,attempt,worker_token,lease_expires_at "
                "FROM conversation_learning_jobs WHERE job_id=$1", bad_job,
            )
            assert row["attempt"] == attempt
            assert row["status"] == ("pending" if attempt < 3 else "failed")
            assert row["worker_token"] is None
            assert row["lease_expires_at"] is None
            if attempt < 3:
                assert await process_one(pool, principal, model=bad_model) is None
                await c.execute(
                    "UPDATE conversation_learning_jobs "
                    "SET available_at=clock_timestamp() WHERE job_id=$1", bad_job,
                )
        assert bad_model.calls == 3
        assert await c.fetchval(
            "SELECT count(*) FROM conversation_memory_proposals WHERE job_id=$1",
            bad_job,
        ) == 0
        assert await process_one(pool, principal, model=bad_model) is None
        print("PASS: delayed retries stop after three failed attempts")
        print("PASS: invalid extraction stores no proposals")

        empty_job = await enqueue(3)
        empty = Model('{"memories": []}')
        result = await process_one(pool, principal, model=empty)
        assert result == {"job_id": str(empty_job), "proposals": 0}
        print("PASS: empty extraction completes without proposals")

        expired = await enqueue(4)
        await c.execute(
            "UPDATE conversation_learning_jobs SET status='running', "
            "attempt=1,worker_token=$2,"
            "lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE job_id=$1", expired, uuid4(),
        )
        result = await process_one(pool, principal, model=model)
        assert result["job_id"] == str(expired)
        assert await c.fetchval(
            "SELECT attempt FROM conversation_learning_jobs WHERE job_id=$1",
            expired,
        ) == 2
        print("PASS: expired lease reclaimed with incremented attempt")

        exhausted = await enqueue(5)
        await c.execute(
            "UPDATE conversation_learning_jobs SET status='running', "
            "attempt=3,worker_token=$2,"
            "lease_expires_at=clock_timestamp()-interval '1 second' "
            "WHERE job_id=$1", exhausted, uuid4(),
        )
        calls = model.calls
        assert await process_one(pool, principal, model=model) is None
        assert model.calls == calls
        assert await c.fetchval(
            "SELECT status FROM conversation_learning_jobs WHERE job_id=$1",
            exhausted,
        ) == "failed"
        print("PASS: exhausted expired lease fails without model call")

        stale = await enqueue(6)
        replacement = uuid4()
        class StaleModel:
            async def complete(self, messages):
                await c.execute(
                    "UPDATE conversation_learning_jobs SET worker_token=$2 "
                    "WHERE job_id=$1", stale, replacement,
                )
                return model.raw
        try:
            await process_one(pool, principal, model=StaleModel())
        except RuntimeError:
            pass
        else:
            raise AssertionError("Stale worker was allowed to finish")
        row = await c.fetchrow(
            "SELECT status,worker_token FROM conversation_learning_jobs "
            "WHERE job_id=$1", stale,
        )
        assert row["status"] == "running"
        assert row["worker_token"] == replacement
        assert await c.fetchval(
            "SELECT count(*) FROM conversation_memory_proposals WHERE job_id=$1",
            stale,
        ) == 0
        await c.execute(
            "UPDATE conversation_learning_jobs SET status='failed', "
            "worker_token=NULL,lease_expires_at=NULL WHERE job_id=$1", stale,
        )
        print("PASS: stale worker token rejected without overwriting new token")

        storage_job = await enqueue(7)
        class FaultConnection:
            def __getattr__(self, name):
                return getattr(c, name)

            async def execute(self, sql, *args):
                if "INSERT INTO conversation_memory_proposals" in sql:
                    raise RuntimeError("injected proposal storage failure")
                return await c.execute(sql, *args)

        class FaultPool:
            @asynccontextmanager
            async def acquire(self):
                yield FaultConnection()

        try:
            await process_one(FaultPool(), principal, model=model)
        except RuntimeError as error:
            assert str(error) == "injected proposal storage failure"
        else:
            raise AssertionError("Injected storage failure was ignored")
        row = await c.fetchrow(
            "SELECT status,completed_at,worker_token "
            "FROM conversation_learning_jobs WHERE job_id=$1", storage_job,
        )
        assert dict(row) == {
            "status": "pending", "completed_at": None, "worker_token": None,
        }
        assert await c.fetchval(
            "SELECT count(*) FROM conversation_memory_proposals WHERE job_id=$1",
            storage_job,
        ) == 0
        print("PASS: storage failure rolls back completion and schedules retry")
    finally:
        try:
            await tx.rollback()
            print("ROLLBACK: test database changes discarded")
        finally:
            await c.close()

asyncio.run(main())
print("DONE L6B: basic worker database checks passed.")
"""
subprocess.run(
    ["docker", "exec", "-i", "atlas-control", "python", "-"],
    input=prefix + checks, text=True, check=True,
)
