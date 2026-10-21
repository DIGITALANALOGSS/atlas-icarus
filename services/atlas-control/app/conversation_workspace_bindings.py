from uuid import UUID, uuid4


class WorkspaceBindingError(RuntimeError):
    pass


async def ensure_workspace_binding(
    connection, principal, conversation_id, workspace_project_id, *, write_event,
):
    """Create or confirm an owner-scoped, immutable workspace association."""
    required = {"conversations:read", "conversations:write"}
    if not required.issubset(principal.permissions):
        raise WorkspaceBindingError("permission denied")
    if workspace_project_id is not None and not isinstance(
        workspace_project_id, UUID
    ):
        raise WorkspaceBindingError("workspace project must be a UUID or null")

    scope = "personal" if workspace_project_id is None else "project"

    async with connection.transaction():
        conversation = await connection.fetchrow(
            """
            SELECT conversation_id, correlation_id
            FROM conversations
            WHERE conversation_id = $1
              AND tenant_id = $2
              AND owner_subject_id = $3
            FOR UPDATE
            """,
            conversation_id, principal.tenant_id, principal.subject_id,
        )
        if conversation is None:
            raise WorkspaceBindingError("conversation unavailable")

        existing = await connection.fetchrow(
            """
            SELECT scope_type, workspace_project_id, bound_by_subject_id
            FROM conversation_workspace_bindings
            WHERE tenant_id = $1 AND conversation_id = $2
            """,
            principal.tenant_id, conversation_id,
        )
        if existing is not None:
            if (
                existing["scope_type"] != scope
                or existing["workspace_project_id"] != workspace_project_id
            ):
                raise WorkspaceBindingError("conversation workspace binding changed")
            return dict(existing)

        inserted = await connection.fetchrow(
            """
            INSERT INTO conversation_workspace_bindings (
                tenant_id, conversation_id, scope_type,
                workspace_project_id, bound_by_subject_id
            )
            VALUES ($1, $2, $3, $4, $5)
            RETURNING scope_type, workspace_project_id, bound_by_subject_id
            """,
            principal.tenant_id, conversation_id, scope,
            workspace_project_id, principal.subject_id,
        )
        if inserted is None:
            raise WorkspaceBindingError("workspace binding was not recorded")

        await write_event(
            connection,
            tenant_id=principal.tenant_id,
            event_type="governance.conversation.workspace.bound",
            correlation_id=conversation["correlation_id"] or uuid4(),
            payload={
                "conversation_id": str(conversation_id),
                "scope_type": scope,
                "workspace_project_id": (
                    str(workspace_project_id)
                    if workspace_project_id is not None else None
                ),
                "bound_by_subject_id": principal.subject_id,
            },
        )
        return dict(inserted)
