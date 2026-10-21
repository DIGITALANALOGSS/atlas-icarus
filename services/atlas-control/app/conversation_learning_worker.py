"""Explicitly invoked proposal worker; importing does not start it."""
from uuid import uuid4
from app.conversation_learning import extract_candidates

async def process_one(pool, principal, *, model):
    required = {"conversations:read", "conversations:write"}
    if not required.issubset(principal.permissions):
        raise PermissionError("permission denied")
    token = uuid4()
    tenant, owner = principal.tenant_id, principal.subject_id
    async with pool.acquire() as c:
        await c.execute("""
            UPDATE conversation_learning_jobs j
            SET status='failed', worker_token=NULL, lease_expires_at=NULL,
                error_code='attempts_exhausted'
            FROM conversations v
            WHERE j.tenant_id=$1 AND v.owner_subject_id=$2
              AND v.tenant_id=j.tenant_id
              AND v.conversation_id=j.conversation_id
              AND j.status='running' AND j.attempt>=3
              AND j.lease_expires_at<=clock_timestamp()
        """, tenant, owner)
        job = await c.fetchrow("""
            WITH picked AS (
              SELECT j.job_id, m.content
              FROM conversation_learning_jobs j
              JOIN conversations v USING (tenant_id, conversation_id)
              JOIN conversation_messages m
                ON m.tenant_id=j.tenant_id
               AND m.conversation_id=j.conversation_id
               AND m.message_id=j.source_message_id
              WHERE j.tenant_id=$1 AND v.owner_subject_id=$2
                AND m.role='user' AND j.attempt<3
                AND ((j.status='pending'
                      AND j.available_at<=clock_timestamp())
                  OR (j.status='running'
                      AND j.lease_expires_at<=clock_timestamp()))
              ORDER BY j.available_at, j.created_at, j.job_id
              LIMIT 1 FOR UPDATE OF j SKIP LOCKED
            )
            UPDATE conversation_learning_jobs j
            SET status='running', attempt=j.attempt+1, worker_token=$3,
                lease_expires_at=clock_timestamp()+interval '60 seconds',
                error_code=NULL
            FROM picked p WHERE j.job_id=p.job_id
            RETURNING j.*, p.content AS source_content
        """, tenant, owner, token)
    if job is None:
        return None
    try:
        proposals = await extract_candidates(
            model, user_text=job["source_content"],
            conversation_id=job["conversation_id"],
            message_id=job["source_message_id"], timeout_seconds=30,
        )
        async with pool.acquire() as c:
            async with c.transaction():
                completed = await c.fetchval("""
                    UPDATE conversation_learning_jobs j
                    SET status='completed', completed_at=clock_timestamp(),
                        worker_token=NULL, lease_expires_at=NULL
                    FROM conversations v, conversation_messages m
                    WHERE j.job_id=$1 AND j.tenant_id=$2
                      AND j.status='running' AND j.worker_token=$3
                      AND j.lease_expires_at>clock_timestamp()
                      AND v.tenant_id=j.tenant_id
                      AND v.conversation_id=j.conversation_id
                      AND v.owner_subject_id=$4
                      AND m.tenant_id=j.tenant_id
                      AND m.conversation_id=j.conversation_id
                      AND m.message_id=j.source_message_id
                      AND m.role='user' AND m.content=$5
                    RETURNING j.job_id
                """, job["job_id"], tenant, token, owner, job["source_content"])
                if completed is None:
                    raise RuntimeError("learning lease or source unavailable")
                for item in proposals:
                    await c.execute("""
                        INSERT INTO conversation_memory_proposals (
                          proposal_id, tenant_id, conversation_id,
                          source_message_id, job_id, category, content
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7)
                        ON CONFLICT (
                          tenant_id, conversation_id, source_message_id,
                          category, content
                        ) DO NOTHING
                    """, uuid4(), tenant, job["conversation_id"],
                        job["source_message_id"], job["job_id"],
                        item["category"], item["content"])
        return {"job_id": str(job["job_id"]), "proposals": len(proposals)}
    except Exception:
        async with pool.acquire() as c:
            await c.execute("""
                UPDATE conversation_learning_jobs
                SET status=CASE WHEN attempt<3 THEN 'pending' ELSE 'failed' END,
                    worker_token=NULL, lease_expires_at=NULL,
                    available_at=clock_timestamp()+interval '30 seconds',
                    error_code='processing_failed'
                WHERE job_id=$1 AND tenant_id=$2
                  AND status='running' AND worker_token=$3
                  AND lease_expires_at>clock_timestamp()
            """, job["job_id"], tenant, token)
        raise
