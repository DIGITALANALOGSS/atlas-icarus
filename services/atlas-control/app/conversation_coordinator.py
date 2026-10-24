import json
from app.conversation_memory import (
    memory_context_digest, stored_memory_context,
)

from uuid import uuid4

from app.auth import Principal
from app.conversation_memory_lifecycle import (
    MemoryLifecycleError, lifecycle_enabled, execution_lock,
    validate_turn_dependencies, snapshot_records, dependency_digest,
)
from app.conversation_memory_recall import (
    accepted_memory_recall_enabled,
    select_accepted_memories,
    merge_memory_context,
)
from app.conversation_workspace_bindings import (
    WorkspaceBindingError, ensure_workspace_binding,
)

WORKSPACE_UNSPECIFIED = object()


class ConversationCoordinatorError(RuntimeError):
    pass


async def _memory_lifecycle_gate(connection, principal, turn):
    try:
        await validate_turn_dependencies(connection, principal, turn)
    except (MemoryLifecycleError, ValueError, TypeError, KeyError) as exc:
        raise ConversationCoordinatorError(
            "turn memory withdrawn, changed, or provenance unavailable"
        ) from exc



async def claim_turn(pool, principal: Principal, turn_id, *, write_event, lease_seconds=90):
    required = {"conversations:read", "conversations:write"}
    if not required.issubset(principal.permissions):
        raise ConversationCoordinatorError("permission denied")
    if type(lease_seconds) is not int or not 1 <= lease_seconds <= 600:
        raise ConversationCoordinatorError("lease must be an integer from 1 to 600 seconds")
    token = uuid4()
    async with pool.acquire() as connection:
        async with connection.transaction():
            await execution_lock(connection, principal)
            row = await connection.fetchrow(
                '''
                UPDATE conversation_turns AS turn
                SET status = 'running', attempt = attempt + 1,
                    worker_token = $4, started_at = clock_timestamp(),
                    lease_expires_at = clock_timestamp() + $5 * INTERVAL '1 second'
                WHERE turn.turn_id = $1 AND turn.tenant_id = $2
                  AND turn.status = 'queued'
                  AND EXISTS (
                    SELECT 1 FROM conversations AS conversation
                    WHERE conversation.conversation_id = turn.conversation_id
                      AND conversation.tenant_id = turn.tenant_id
                      AND conversation.owner_subject_id = $3
                  )
                RETURNING turn.*
                ''',
                turn_id, principal.tenant_id, principal.subject_id, token, lease_seconds,
            )
            if row is None:
                raise ConversationCoordinatorError("turn unavailable")
            await _memory_lifecycle_gate(connection, principal, dict(row))
            await write_event(
                connection,
                tenant_id=principal.tenant_id,
                event_type="governance.conversation.turn.running",
                correlation_id=row["correlation_id"],
                payload={
                    "turn_id": str(row["turn_id"]),
                    "conversation_id": str(row["conversation_id"]),
                    "attempt": row["attempt"],
                },
            )
            return dict(row)


async def cancel_turn(pool, principal, turn_id, *, write_event):
    return await _finish_turn(
        pool, principal, turn_id, status="cancelled", write_event=write_event,
    )


async def fail_turn(
    pool, principal, turn_id, *, worker_token, error_code, write_event,
):
    if worker_token is None:
        raise ConversationCoordinatorError("worker token required")
    if (
        not isinstance(error_code, str)
        or not error_code.strip()
        or len(error_code) > 128
    ):
        raise ConversationCoordinatorError("error code must contain 1 to 128 characters")
    return await _finish_turn(
        pool, principal, turn_id, status="failed", write_event=write_event,
        worker_token=worker_token, error_code=error_code.strip(),
    )


async def _finish_turn(
    pool, principal, turn_id, *, status, write_event,
    worker_token=None, error_code=None,
):
    required = {"conversations:read", "conversations:write"}
    if not required.issubset(principal.permissions):
        raise ConversationCoordinatorError("permission denied")
    async with pool.acquire() as connection:
        async with connection.transaction():
            existing = await connection.fetchrow(
                """
                SELECT turn.*
                FROM conversation_turns AS turn
                JOIN conversations AS conversation
                  ON conversation.tenant_id = turn.tenant_id
                 AND conversation.conversation_id = turn.conversation_id
                WHERE turn.turn_id = $1 AND turn.tenant_id = $2
                  AND conversation.owner_subject_id = $3
                FOR UPDATE OF turn
                """,
                turn_id, principal.tenant_id, principal.subject_id,
            )
            if existing is None:
                raise ConversationCoordinatorError("turn unavailable")
            row = await connection.fetchrow(
                """
                UPDATE conversation_turns
                SET status = $3, completed_at = clock_timestamp(),
                    worker_token = NULL, lease_expires_at = NULL,
                    error_code = $5
                WHERE turn_id = $1 AND tenant_id = $2
                  AND (
                    ($3 = 'cancelled' AND status IN ('queued', 'running'))
                    OR
                    ($3 = 'failed' AND status = 'running'
                      AND worker_token = $4
                      AND lease_expires_at > clock_timestamp())
                  )
                RETURNING *
                """,
                turn_id, principal.tenant_id, status, worker_token, error_code,
            )
            if row is None:
                raise ConversationCoordinatorError("turn unavailable")
            await write_event(
                connection,
                tenant_id=principal.tenant_id,
                event_type="governance.conversation.turn." + status,
                correlation_id=row["correlation_id"],
                payload={
                    "turn_id": str(row["turn_id"]),
                    "conversation_id": str(row["conversation_id"]),
                    "attempt": row["attempt"],
                    **({"error_code": error_code} if status == "failed" else {}),
                },
            )
            return dict(row)


async def publish_turn(pool, principal, turn_id, *, worker_token, content, write_event, tool_calls=()):
    required = {'conversations:read', 'conversations:write'}
    if not required.issubset(principal.permissions):
        raise ConversationCoordinatorError('permission denied')
    if worker_token is None:
        raise ConversationCoordinatorError('worker token required')
    if tool_calls:
        if not isinstance(tool_calls, tuple) or len(tool_calls) != 1:
            raise ConversationCoordinatorError('exactly one tool proposal required')
        if 'jobs:create' not in principal.permissions:
            raise ConversationCoordinatorError('permission denied')
    elif not isinstance(content, str) or not content.strip():
        raise ConversationCoordinatorError('assistant content must not be blank')
    async with pool.acquire() as connection:
        async with connection.transaction():
            await execution_lock(connection, principal)
            turn = await connection.fetchrow("\n                SELECT turn.*\n                FROM conversation_turns AS turn\n                JOIN conversations AS conversation\n                  ON conversation.tenant_id = turn.tenant_id\n                 AND conversation.conversation_id = turn.conversation_id\n                WHERE turn.turn_id = $1 AND turn.tenant_id = $2\n                  AND conversation.owner_subject_id = $3\n                  AND turn.status = 'running'\n                  AND turn.worker_token = $4\n                  AND turn.lease_expires_at > clock_timestamp()\n                FOR UPDATE OF turn\n                ", turn_id, principal.tenant_id, principal.subject_id, worker_token)
            if turn is None:
                raise ConversationCoordinatorError('turn unavailable')
            await _memory_lifecycle_gate(connection, principal, dict(turn))
            conversation = await connection.fetchrow('\n                SELECT conversation_id FROM conversations\n                WHERE conversation_id = $1 AND tenant_id = $2\n                  AND owner_subject_id = $3\n                FOR UPDATE\n                ', turn['conversation_id'], principal.tenant_id, principal.subject_id)
            if conversation is None:
                raise ConversationCoordinatorError('turn unavailable')
            user_role = await connection.fetchval('\n                SELECT role FROM conversation_messages\n                WHERE message_id = $1 AND tenant_id = $2\n                  AND conversation_id = $3\n                ', turn['user_message_id'], principal.tenant_id, turn['conversation_id'])
            if user_role != 'user':
                raise ConversationCoordinatorError('turn source must be a user message')
            if tool_calls:
                if getattr(tool_calls[0], 'name', None) == 'atlas_job_status':
                    from app.conversation_tool_read import read_tool_job
                    content = await read_tool_job(connection, principal, dict(turn), tool_calls[0])
                else:
                    from app.conversation_tool_jobs import prepare_tool_job
                    proposed_job = await prepare_tool_job(connection, principal, dict(turn), tool_calls[0])
                    content = 'Prepared metadata analysis job ' + proposed_job['job_id'] + '. Approval gate: ' + proposed_job['approval_gate_id'] + '. It is pending approval and has not been executed.'
            message = await connection.fetchrow("\n                INSERT INTO conversation_messages (\n                    message_id, tenant_id, conversation_id,\n                    sequence_number, role, content\n                )\n                SELECT $1, $2, $3, COALESCE(MAX(sequence_number), 0) + 1,\n                       'assistant', $4\n                FROM conversation_messages\n                WHERE conversation_id = $3 AND tenant_id = $2\n                RETURNING message_id, conversation_id, sequence_number,\n                          role, content, client_message_id, created_at\n                ", uuid4(), principal.tenant_id, turn['conversation_id'], content)
            completed = await connection.fetchrow("\n                UPDATE conversation_turns\n                SET status = 'completed', assistant_message_id = $4,\n                    completed_at = clock_timestamp(),\n                    worker_token = NULL, lease_expires_at = NULL\n                WHERE turn_id = $1 AND tenant_id = $2\n                  AND status = 'running' AND worker_token = $3\n                  AND lease_expires_at > clock_timestamp()\n                RETURNING *\n                ", turn_id, principal.tenant_id, worker_token, message['message_id'])
            if completed is None:
                raise ConversationCoordinatorError('turn unavailable')
            await connection.execute('\n                INSERT INTO conversation_learning_jobs (\n                    job_id, tenant_id, conversation_id, source_message_id\n                )\n                VALUES ($1, $2, $3, $4)\n                ON CONFLICT (\n                    tenant_id, conversation_id, source_message_id\n                ) DO NOTHING\n                ', uuid4(), principal.tenant_id, turn['conversation_id'], turn['user_message_id'])
            await connection.execute('\n                UPDATE conversations SET updated_at = clock_timestamp()\n                WHERE conversation_id = $1 AND tenant_id = $2\n                  AND owner_subject_id = $3\n                ', turn['conversation_id'], principal.tenant_id, principal.subject_id)
            await write_event(connection, tenant_id=principal.tenant_id, event_type='governance.conversation.turn.completed', correlation_id=turn['correlation_id'], payload={'turn_id': str(turn_id), 'conversation_id': str(turn['conversation_id']), 'assistant_message_id': str(message['message_id']), 'attempt': completed['attempt']})
            return {'turn': dict(completed), 'message': dict(message)}


def context_digest(messages):
    import hashlib
    import json

    snapshot = [
        [str(row["message_id"]), row["sequence_number"], row["role"], row["content"]]
        for row in messages
    ]
    encoded = json.dumps(
        snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def reserve_turn(
    pool, principal, conversation_id, user_message_id, *, write_event,
    memory_context=(), workspace_project_id=WORKSPACE_UNSPECIFIED,
):
    required = {"conversations:read", "conversations:write"}
    if not required.issubset(principal.permissions):
        raise ConversationCoordinatorError("permission denied")
    memory_context_digest(memory_context)
    selected_memory = list(memory_context)
    memory_sha256 = memory_context_digest(selected_memory)
    recall_enabled = accepted_memory_recall_enabled()
    enforce_lifecycle = lifecycle_enabled()
    if enforce_lifecycle and not recall_enabled:
        raise ConversationCoordinatorError(
            "lifecycle enforcement requires accepted-memory recall"
        )
    dependencies = None
    async with pool.acquire() as connection:
        async with connection.transaction():
            await execution_lock(connection, principal)
            conversation = await connection.fetchrow(
                """
                SELECT conversation_id FROM conversations
                WHERE conversation_id = $1 AND tenant_id = $2
                  AND owner_subject_id = $3
                FOR UPDATE
                """,
                conversation_id, principal.tenant_id, principal.subject_id,
            )
            if conversation is None:
                raise ConversationCoordinatorError("conversation unavailable")
            user = await connection.fetchrow(
                """
                SELECT role, sequence_number FROM conversation_messages
                WHERE message_id = $1 AND tenant_id = $2
                  AND conversation_id = $3
                """,
                user_message_id, principal.tenant_id, conversation_id,
            )
            if user is None or user["role"] != "user":
                raise ConversationCoordinatorError("user message unavailable")
            if workspace_project_id is not WORKSPACE_UNSPECIFIED:
                try:
                    await ensure_workspace_binding(
                        connection, principal, conversation_id,
                        workspace_project_id, write_event=write_event,
                    )
                except WorkspaceBindingError as exc:
                    raise ConversationCoordinatorError(str(exc)) from exc
            existing = await connection.fetchrow(
                """
                SELECT * FROM conversation_turns
                WHERE tenant_id = $1 AND conversation_id = $2
                  AND user_message_id = $3
                """,
                principal.tenant_id, conversation_id, user_message_id,
            )
            if existing is not None:
                if existing['status'] not in {
                    'completed', 'failed', 'cancelled'
                }:
                    try:
                        saved = stored_memory_context(existing)
                        await _memory_lifecycle_gate(
                            connection, principal, dict(existing),
                        )
                        if (
                            not recall_enabled
                            and memory_context_digest(saved) != memory_sha256
                        ):
                            raise ValueError('memory selection changed')
                    except (ValueError, TypeError) as exc:
                        raise ConversationCoordinatorError(
                            'turn memory context unavailable or changed'
                        ) from exc
                return dict(existing)
            active = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM conversation_turns
                    WHERE tenant_id = $1 AND conversation_id = $2
                      AND status IN ('queued', 'running')
                )
                """,
                principal.tenant_id, conversation_id,
            )
            if active:
                raise ConversationCoordinatorError("conversation has an active turn")
            if enforce_lifecycle:
                if selected_memory:
                    raise ConversationCoordinatorError(
                        "untracked saved context is not allowed in lifecycle enforcement mode"
                    )
                records = await select_accepted_memories(
                    connection, principal, conversation_id,
                    with_provenance=True,
                )
                selected_memory, dependencies = snapshot_records(records)
                memory_sha256 = memory_context_digest(selected_memory)
            elif recall_enabled:
                recalled = await select_accepted_memories(
                    connection, principal, conversation_id,
                )
                selected_memory = merge_memory_context(
                    recalled, selected_memory,
                )
                memory_sha256 = memory_context_digest(selected_memory)
            messages = await connection.fetch(
                """
                SELECT message_id, sequence_number, role, content
                FROM conversation_messages
                WHERE tenant_id = $1 AND conversation_id = $2
                  AND sequence_number <= $3
                ORDER BY sequence_number, message_id
                """,
                principal.tenant_id, conversation_id, user["sequence_number"],
            )
            turn_id = uuid4()
            row = await connection.fetchrow(
                """
                INSERT INTO conversation_turns (
                    turn_id, tenant_id, conversation_id, user_message_id,
                    correlation_id, context_sequence_number, context_sha256,
                    memory_context, memory_context_sha256, status
                )
                VALUES ($1, $2, $3, $4, $1, $5, $6, $7::jsonb, $8, 'queued')
                RETURNING *
                """,
                turn_id, principal.tenant_id, conversation_id, user_message_id,
                user["sequence_number"], context_digest(messages),
                json.dumps(selected_memory, ensure_ascii=False),
                memory_sha256,
            )
            if dependencies is not None:
                row = await connection.fetchrow(
                    """
                    UPDATE conversation_turns
                    SET memory_dependencies=$3::jsonb,
                        memory_dependencies_sha256=$4
                    WHERE turn_id=$1 AND tenant_id=$2
                    RETURNING *
                    """,
                    turn_id, principal.tenant_id,
                    json.dumps(dependencies),
                    dependency_digest(dependencies),
                )
                if row is None:
                    raise ConversationCoordinatorError("turn unavailable")
            await write_event(
                connection,
                tenant_id=principal.tenant_id,
                event_type="governance.conversation.turn.queued",
                correlation_id=row["correlation_id"],
                payload={
                    "turn_id": str(turn_id),
                    "conversation_id": str(conversation_id),
                    "user_message_id": str(user_message_id),
                    "context_sequence_number": row["context_sequence_number"],
                    "context_sha256": row["context_sha256"],
                    "memory_context_sha256": memory_sha256,
                    "memory_count": len(selected_memory),
                },
            )
            return dict(row)


async def _load_turn_context(
    pool, principal, turn_id, worker_token, *,
    expected_memory_sha256=None,
):
    async with pool.acquire() as connection:
        async with connection.transaction():
            await execution_lock(connection, principal)
            turn = await connection.fetchrow(
                """
                SELECT turn.*
                FROM conversation_turns AS turn
                JOIN conversations AS conversation
                  ON conversation.tenant_id = turn.tenant_id
                 AND conversation.conversation_id = turn.conversation_id
                WHERE turn.turn_id = $1 AND turn.tenant_id = $2
                  AND conversation.owner_subject_id = $3
                  AND turn.status = 'running' AND turn.worker_token = $4
                  AND turn.lease_expires_at > clock_timestamp()
                """,
                turn_id, principal.tenant_id, principal.subject_id, worker_token,
            )
            if turn is None:
                raise ConversationCoordinatorError("turn unavailable")
            await _memory_lifecycle_gate(connection, principal, dict(turn))
            try:
                stored_memory_context(turn)
                if (
                    expected_memory_sha256 is not None
                    and turn["memory_context_sha256"] != expected_memory_sha256
                ):
                    raise ValueError("memory selection changed")
            except (ValueError, TypeError) as exc:
                raise ConversationCoordinatorError(
                    "turn memory context unavailable or changed"
                ) from exc
            messages = await connection.fetch(
                """
                SELECT message_id, sequence_number, role, content
                FROM conversation_messages
                WHERE tenant_id = $1 AND conversation_id = $2
                  AND sequence_number <= $3
                ORDER BY sequence_number, message_id
                """,
                principal.tenant_id, turn["conversation_id"],
                turn["context_sequence_number"],
            )
            if (
                not messages
                or messages[-1]["message_id"] != turn["user_message_id"]
                or messages[-1]["role"] != "user"
                or messages[-1]["sequence_number"] != turn["context_sequence_number"]
                or context_digest(messages) != turn["context_sha256"]
            ):
                raise ConversationCoordinatorError("turn context mismatch")
            remaining = await connection.fetchval(
                """
                SELECT EXTRACT(EPOCH FROM lease_expires_at - clock_timestamp())
                FROM conversation_turns
                WHERE turn_id = $1 AND tenant_id = $2
                  AND status = 'running' AND worker_token = $3
                """,
                turn_id, principal.tenant_id, worker_token,
            )
            if remaining is None or remaining <= 0:
                raise ConversationCoordinatorError("turn unavailable")
            return list(messages), float(remaining)


async def execute_turn(pool, principal, conversation_id, user_message_id, *, model, write_event, lease_seconds=90, memory_context=(), workspace_project_id=WORKSPACE_UNSPECIFIED, tool_mode=False):
    import asyncio
    from app.conversation_adapters import ConversationTurn
    from app.conversation_memory import memory_system_message
    if type(tool_mode) is not bool:
        raise ConversationCoordinatorError('tool mode must be boolean')
    if tool_mode:
        if 'jobs:create' not in principal.permissions:
            raise ConversationCoordinatorError('permission denied')
        if not callable(getattr(model, 'complete_with_tools', None)):
            raise ConversationCoordinatorError('structured model required')
    memory_system_message(memory_context)
    selected_memory = list(memory_context)
    workspace_options = {}
    if workspace_project_id is not WORKSPACE_UNSPECIFIED:
        workspace_options['workspace_project_id'] = workspace_project_id
    reserved = await reserve_turn(pool, principal, conversation_id, user_message_id, write_event=write_event, memory_context=selected_memory, **workspace_options)
    if reserved['status'] == 'completed':
        async with pool.acquire() as connection:
            message = await connection.fetchrow("\n                SELECT message_id, conversation_id, sequence_number,\n                       role, content, client_message_id, created_at\n                FROM conversation_messages\n                WHERE tenant_id = $1 AND conversation_id = $2\n                  AND message_id = $3 AND role = 'assistant'\n                ", principal.tenant_id, conversation_id, reserved['assistant_message_id'])
            if message is None:
                raise ConversationCoordinatorError('completed result unavailable')
            return {'turn': reserved, 'message': dict(message)}
    if reserved['status'] in {'failed', 'cancelled'}:
        raise ConversationCoordinatorError('turn is terminal')
    try:
        memory_prompt = memory_system_message(stored_memory_context(reserved))
    except (ValueError, TypeError) as exc:
        raise ConversationCoordinatorError('turn memory context unavailable or changed') from exc
    claimed = await claim_turn(pool, principal, reserved['turn_id'], write_event=write_event, lease_seconds=lease_seconds)
    turn_id = claimed['turn_id']
    token = claimed['worker_token']
    try:
        (rows, remaining) = await _load_turn_context(pool, principal, turn_id, token, expected_memory_sha256=reserved['memory_context_sha256'])
        context = [ConversationTurn(row['role'], row['content']) for row in rows]
        if memory_prompt is not None:
            context.insert(0, ConversationTurn('system', memory_prompt))
        tool_calls = ()
        if tool_mode:
            from app.conversation_tool_jobs import proposal_tools
            from app.conversation_tool_calls import StructuredModelReply
            reply = await asyncio.wait_for(model.complete_with_tools(context, tools=proposal_tools()), timeout=remaining)
            if not isinstance(reply, StructuredModelReply):
                raise ConversationCoordinatorError('invalid structured model reply')
            (content, tool_calls) = (reply.content, reply.tool_calls)
        else:
            content = await asyncio.wait_for(model.complete(context), timeout=remaining)
        return await publish_turn(pool, principal, turn_id, worker_token=token, content=content, write_event=write_event, tool_calls=tool_calls)
    except Exception as error:
        code = 'execution_timeout' if isinstance(error, TimeoutError) else 'execution_failed'
        try:
            await fail_turn(pool, principal, turn_id, worker_token=token, error_code=code, write_event=write_event)
        except Exception as finalization_error:
            error.add_note('Failure finalization did not succeed: ' + type(finalization_error).__name__)
        raise


async def expire_conversation_turns(
    pool, principal, conversation_id, *, write_event,
):
    if "conversations:read" not in principal.permissions:
        raise ConversationCoordinatorError("permission denied")
    async with pool.acquire() as connection:
        async with connection.transaction():
            rows = await connection.fetch(
                """
                UPDATE conversation_turns AS turn
                SET status = 'failed', error_code = 'lease_expired',
                    completed_at = clock_timestamp(),
                    worker_token = NULL, lease_expires_at = NULL
                WHERE turn.tenant_id = $1 AND turn.conversation_id = $2
                  AND turn.status = 'running'
                  AND turn.lease_expires_at <= clock_timestamp()
                  AND EXISTS (
                    SELECT 1 FROM conversations AS conversation
                    WHERE conversation.tenant_id = turn.tenant_id
                      AND conversation.conversation_id = turn.conversation_id
                      AND conversation.owner_subject_id = $3
                  )
                RETURNING turn.*
                """,
                principal.tenant_id, conversation_id, principal.subject_id,
            )
            for row in rows:
                await write_event(
                    connection,
                    tenant_id=principal.tenant_id,
                    event_type="governance.conversation.turn.failed",
                    correlation_id=row["correlation_id"],
                    payload={
                        "turn_id": str(row["turn_id"]),
                        "conversation_id": str(conversation_id),
                        "attempt": row["attempt"],
                        "error_code": "lease_expired",
                    },
                )
            return [dict(row) for row in rows]
