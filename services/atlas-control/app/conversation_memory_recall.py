"""Owner-scoped accepted-memory selection for new turn snapshots."""
import os

from app.conversation_memory import memory_system_message


def accepted_memory_recall_enabled():
    return os.environ.get("ATLAS_ACCEPTED_MEMORY_RECALL") == "1"


def merge_memory_context(accepted, supplied):
    memory_system_message(accepted)
    memory_system_message(supplied)
    result = []
    seen = set()
    for content in list(accepted) + list(supplied):
        if content not in seen:
            seen.add(content)
            result.append(content)
        if len(result) == 12:
            break
    return result


async def select_accepted_memories(
    connection, principal, conversation_id, *, with_provenance=False,
):
    required = {"conversations:read", "conversations:write"}
    if not required.issubset(principal.permissions):
        raise PermissionError("permission denied")

    rows = await connection.fetch(
        """
        SELECT p.proposal_id, p.content
        FROM conversation_memory_proposals AS p
        JOIN conversations AS source
          ON source.tenant_id = p.tenant_id
         AND source.conversation_id = p.conversation_id
        JOIN conversations AS target
          ON target.conversation_id = $1
         AND target.tenant_id = $2
         AND target.owner_subject_id = $3
        LEFT JOIN conversation_workspace_bindings AS destination_binding
          ON destination_binding.tenant_id = target.tenant_id
         AND destination_binding.conversation_id = target.conversation_id
        LEFT JOIN conversation_workspace_bindings AS source_binding
          ON source_binding.tenant_id = source.tenant_id
         AND source_binding.conversation_id = source.conversation_id
        WHERE p.tenant_id = $2
          AND source.owner_subject_id = $3
          AND p.status = 'accepted'
          AND p.lifecycle_state = 'active'
          AND p.reviewed_by_subject_id = $3
          AND p.reviewed_at IS NOT NULL
          AND (
            (
              p.memory_scope = 'conversation'
              AND p.conversation_id = target.conversation_id
              AND p.workspace_project_id IS NULL
            )
            OR
            (
              p.memory_scope = 'personal'
              AND p.workspace_project_id IS NULL
            )
            OR
            (
              p.memory_scope = 'project'
              AND destination_binding.scope_type = 'project'
              AND source_binding.scope_type = 'project'
              AND p.workspace_project_id =
                  destination_binding.workspace_project_id
              AND p.workspace_project_id =
                  source_binding.workspace_project_id
            )
          )
        ORDER BY
          CASE p.memory_scope
            WHEN 'conversation' THEN 0
            WHEN 'project' THEN 1
            ELSE 2
          END,
          p.reviewed_at DESC,
          p.proposal_id ASC
        LIMIT 12
        """,
        conversation_id, principal.tenant_id, principal.subject_id,
    )
    memories = [row["content"] for row in rows]
    memory_system_message(memories)
    if with_provenance:
        return [dict(row) for row in rows]
    return merge_memory_context(memories, [])
