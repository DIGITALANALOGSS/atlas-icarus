"""Memory lifecycle helpers; integration is pending."""
import hashlib
import json
import os
from uuid import UUID, uuid4

from app.conversation_memory import memory_system_message, stored_memory_context


class MemoryLifecycleError(RuntimeError):
    pass


def lifecycle_enabled():
    return os.environ.get("ATLAS_MEMORY_LIFECYCLE_ENFORCEMENT") == "1"


def require_permissions(principal):
    required = {"conversations:read", "conversations:write"}
    if not required.issubset(principal.permissions):
        raise MemoryLifecycleError("permission denied")


def dependency_digest(records):
    encoded = json.dumps(
        records, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def snapshot_records(rows):
    if len(rows) > 12:
        raise MemoryLifecycleError("too many memory dependencies")
    memories, dependencies, seen = [], [], set()
    for row in rows:
        content = row["content"]
        memory_system_message([content])
        pid = UUID(str(row["proposal_id"]))
        if content in seen:
            continue
        seen.add(content)
        memories.append(content)
        dependencies.append({
            "proposal_id": str(pid),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        })
    memory_system_message(memories)
    return memories, dependencies


async def lock_owner(connection, principal):
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))",
        str(principal.tenant_id) + ":" + principal.subject_id,
    )


async def execution_lock(connection, principal):
    if lifecycle_enabled():
        await lock_owner(connection, principal)


async def validate_turn_dependencies(connection, principal, turn):
    raw = turn.get("memory_dependencies")
    expected = turn.get("memory_dependencies_sha256")
    if not lifecycle_enabled():
        if raw is not None or expected is not None:
            raise MemoryLifecycleError("memory lifecycle enforcement required")
        return

    require_permissions(principal)
    memories = stored_memory_context(turn)
    if raw is None:
        if memories or expected is not None:
            raise MemoryLifecycleError("turn memory provenance unavailable")
        return

    if isinstance(raw, str):
        raw = json.loads(raw)
    if (
        not isinstance(raw, list)
        or len(raw) > 12
        or len(raw) != len(memories)
        or not isinstance(expected, str)
        or dependency_digest(raw) != expected
    ):
        raise MemoryLifecycleError("turn memory provenance mismatch")

    seen = set()
    for record, saved in zip(raw, memories):
        if not isinstance(record, dict) or set(record) != {
            "proposal_id", "content_sha256"
        }:
            raise MemoryLifecycleError("turn memory provenance mismatch")
        try:
            pid = UUID(record["proposal_id"])
        except (ValueError, TypeError, AttributeError) as exc:
            raise MemoryLifecycleError("turn memory provenance mismatch") from exc
        if pid in seen:
            raise MemoryLifecycleError("turn memory provenance mismatch")
        seen.add(pid)

        row = await connection.fetchrow(
            """
            SELECT p.content
            FROM conversation_memory_proposals AS p
            JOIN conversations AS source
              ON source.tenant_id=p.tenant_id
             AND source.conversation_id=p.conversation_id
            JOIN conversations AS target
              ON target.tenant_id=$2 AND target.conversation_id=$4
             AND target.owner_subject_id=$3
            WHERE p.proposal_id=$1 AND p.tenant_id=$2
              AND source.owner_subject_id=$3
              AND p.reviewed_by_subject_id=$3
              AND p.reviewed_at IS NOT NULL
              AND p.status='accepted' AND p.lifecycle_state='active'
              AND (
                (p.memory_scope='conversation'
                 AND p.conversation_id=target.conversation_id
                 AND p.workspace_project_id IS NULL)
                OR
                (p.memory_scope='personal' AND p.workspace_project_id IS NULL)
                OR
                (p.memory_scope='project' AND EXISTS (
                  SELECT 1
                  FROM conversation_workspace_bindings AS destination
                  JOIN conversation_workspace_bindings AS origin
                    ON origin.tenant_id=destination.tenant_id
                   AND origin.workspace_project_id=destination.workspace_project_id
                  WHERE destination.tenant_id=p.tenant_id
                    AND destination.conversation_id=target.conversation_id
                    AND origin.conversation_id=source.conversation_id
                    AND destination.scope_type='project'
                    AND origin.scope_type='project'
                    AND destination.workspace_project_id=p.workspace_project_id
                ))
              )
            FOR SHARE OF p
            """,
            pid, principal.tenant_id, principal.subject_id,
            turn["conversation_id"],
        )
        actual_hash = hashlib.sha256(saved.encode("utf-8")).hexdigest()
        if (
            row is None
            or row["content"] != saved
            or record["content_sha256"] != actual_hash
        ):
            raise MemoryLifecycleError("turn memory withdrawn or changed")


async def _lock_owned_proposals(connection, principal, ids):
    require_permissions(principal)
    await lock_owner(connection, principal)
    rows = await connection.fetch(
        """
        SELECT p.proposal_id,p.conversation_id
        FROM conversation_memory_proposals AS p
        JOIN conversations AS c
          ON c.tenant_id=p.tenant_id AND c.conversation_id=p.conversation_id
        WHERE p.tenant_id=$1 AND c.owner_subject_id=$2
          AND p.proposal_id=ANY($3::uuid[])
        """,
        principal.tenant_id, principal.subject_id, ids,
    )
    if len(rows) != len(ids):
        raise MemoryLifecycleError("memory unavailable")

    conversation_ids = sorted({r["conversation_id"] for r in rows}, key=str)
    locked = await connection.fetch(
        """
        SELECT conversation_id FROM conversations
        WHERE tenant_id=$1 AND owner_subject_id=$2
          AND conversation_id=ANY($3::uuid[])
        ORDER BY conversation_id FOR UPDATE
        """,
        principal.tenant_id, principal.subject_id, conversation_ids,
    )
    if len(locked) != len(conversation_ids):
        raise MemoryLifecycleError("memory unavailable")

    records = await connection.fetch(
        """
        SELECT p.*
        FROM conversation_memory_proposals AS p
        JOIN conversations AS c
          ON c.tenant_id=p.tenant_id AND c.conversation_id=p.conversation_id
        WHERE p.tenant_id=$1 AND c.owner_subject_id=$2
          AND p.proposal_id=ANY($3::uuid[])
        ORDER BY p.proposal_id FOR UPDATE OF p
        """,
        principal.tenant_id, principal.subject_id, ids,
    )
    if len(records) != len(ids):
        raise MemoryLifecycleError("memory unavailable")
    return {r["proposal_id"]: r for r in records}


async def _accept_memory_replacement(connection, principal, old, new):
    if new["status"] != "proposed" or new["lifecycle_state"] != "active":
        raise MemoryLifecycleError("replacement must be an active proposal")
    if (
        old["memory_scope"] == "conversation"
        and old["conversation_id"] != new["conversation_id"]
    ):
        raise MemoryLifecycleError("conversation scope cannot move")
    if old["memory_scope"] == "project":
        conversations = sorted(
            {old["conversation_id"], new["conversation_id"]}, key=str,
        )
        count = await connection.fetchval(
            """
            SELECT count(*) FROM conversation_workspace_bindings
            WHERE tenant_id=$1 AND scope_type='project'
              AND workspace_project_id=$2
              AND conversation_id=ANY($3::uuid[])
            """,
            principal.tenant_id, old["workspace_project_id"], conversations,
        )
        if count != len(conversations):
            raise MemoryLifecycleError("replacement project binding mismatch")

    updated = await connection.fetchrow(
        """
        UPDATE conversation_memory_proposals
        SET status='accepted',reviewed_at=NOW(),reviewed_by_subject_id=$3,
            memory_scope=$4,workspace_project_id=$5
        WHERE tenant_id=$1 AND proposal_id=$2
          AND status='proposed' AND lifecycle_state='active'
        RETURNING proposal_id
        """,
        principal.tenant_id, new["proposal_id"], principal.subject_id,
        old["memory_scope"], old["workspace_project_id"],
    )
    if updated is None:
        raise MemoryLifecycleError("replacement changed during approval")


async def transition_memory(
    pool, principal, proposal_id, *,
    reason, write_event, replacement_proposal_id=None,
):
    require_permissions(principal)
    if not lifecycle_enabled():
        raise MemoryLifecycleError("memory lifecycle enforcement is not enabled")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise MemoryLifecycleError("reason must contain 1 to 1000 characters")
    if not isinstance(proposal_id, UUID):
        raise MemoryLifecycleError("proposal must be a UUID")
    if replacement_proposal_id is not None:
        if not isinstance(replacement_proposal_id, UUID):
            raise MemoryLifecycleError("replacement must be a UUID")
        if replacement_proposal_id == proposal_id:
            raise MemoryLifecycleError("memory cannot replace itself")

    action = "revoked" if replacement_proposal_id is None else "superseded"
    ids = [proposal_id]
    if replacement_proposal_id is not None:
        ids.append(replacement_proposal_id)

    async with pool.acquire() as connection:
        async with connection.transaction():
            records = await _lock_owned_proposals(connection, principal, ids)
            old = records[proposal_id]
            prior = await connection.fetchrow(
                "SELECT * FROM conversation_memory_transitions "
                "WHERE tenant_id=$1 AND proposal_id=$2",
                principal.tenant_id, proposal_id,
            )
            if prior is not None:
                if (
                    prior["action"] != action
                    or prior["replacement_proposal_id"] != replacement_proposal_id
                    or prior["reason"] != reason
                    or prior["actor_subject_id"] != principal.subject_id
                    or old["lifecycle_state"] != action
                    or old["superseded_by_proposal_id"] != replacement_proposal_id
                ):
                    raise MemoryLifecycleError("memory transition conflicts")
                return dict(prior)

            if old["status"] != "accepted" or old["lifecycle_state"] != "active":
                raise MemoryLifecycleError("memory is not active and accepted")
            if replacement_proposal_id is not None:
                await _accept_memory_replacement(
                    connection, principal, old, records[replacement_proposal_id],
                )

            changed = await connection.fetchrow(
                """
                UPDATE conversation_memory_proposals
                SET lifecycle_state=$3,lifecycle_changed_at=NOW(),
                    lifecycle_changed_by_subject_id=$4,
                    superseded_by_proposal_id=$5
                WHERE tenant_id=$1 AND proposal_id=$2
                  AND status='accepted' AND lifecycle_state='active'
                RETURNING proposal_id
                """,
                principal.tenant_id, proposal_id, action,
                principal.subject_id, replacement_proposal_id,
            )
            if changed is None:
                raise MemoryLifecycleError("memory changed during transition")

            transition = await connection.fetchrow(
                """
                INSERT INTO conversation_memory_transitions (
                    transition_id,tenant_id,proposal_id,action,
                    replacement_proposal_id,actor_subject_id,reason
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING *
                """,
                uuid4(), principal.tenant_id, proposal_id, action,
                replacement_proposal_id, principal.subject_id, reason,
            )
            if transition is None:
                raise MemoryLifecycleError("memory transition was not recorded")

            await write_event(
                connection,
                tenant_id=principal.tenant_id,
                event_type="governance.conversation.memory." + action,
                correlation_id=transition["transition_id"],
                payload={
                    "transition_id": str(transition["transition_id"]),
                    "proposal_id": str(proposal_id),
                    "replacement_proposal_id": (
                        str(replacement_proposal_id)
                        if replacement_proposal_id is not None else None
                    ),
                    "actor_subject_id": principal.subject_id,
                    "memory_scope": old["memory_scope"],
                },
            )
            return dict(transition)
