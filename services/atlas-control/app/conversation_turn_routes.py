import os
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator
from app.conversation_memory import memory_system_message

from app.auth import Principal, require_permission
from app.conversation_adapters import (
    ConversationModelError, EchoConversationModel, LlamaCppConversationModel,
)
from app.conversation_coordinator import (
    ConversationCoordinatorError, cancel_turn, execute_turn,
    expire_conversation_turns,
)


class ConversationTurnCreate(BaseModel):
    model_config = {"extra": "forbid"}
    user_message_id: UUID
    workspace_project_id: UUID | None = None
    memory_context: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("memory_context")
    @classmethod
    def validate_memory_context(cls, value):
        memory_system_message(value)
        return value


def configured_tool_proposals():
    value = os.environ.get("ATLAS_CONVERSATION_TOOL_PROPOSALS", "0").strip()
    if value not in {"0", "1"}:
        raise RuntimeError("ATLAS_CONVERSATION_TOOL_PROPOSALS must be 0 or 1")
    return value == "1"

def configured_conversation_model():
    enabled = configured_tool_proposals()
    provider = os.environ.get("ATLAS_CONVERSATION_MODEL")
    if provider == "echo":
        if enabled:
            raise RuntimeError("Tool proposals require llama_cpp")
        return EchoConversationModel()
    if provider == "llama_cpp":
        if enabled:
            from app.conversation_tool_calls import ToolCapableConversationModel
            return ToolCapableConversationModel.from_environment()
        return LlamaCppConversationModel.from_environment()
    raise RuntimeError("Set ATLAS_CONVERSATION_MODEL to echo or llama_cpp")


def public_turn(row):
    fields = (
        "turn_id", "conversation_id", "user_message_id", "assistant_message_id",
        "status", "attempt", "error_code", "correlation_id",
        "created_at", "started_at", "completed_at",
    )
    return jsonable_encoder({field: row[field] for field in fields})


def turn_http_error(error):
    if isinstance(error, TimeoutError):
        return HTTPException(504, "conversation execution timed out")
    if isinstance(error, ConversationModelError):
        return HTTPException(502, "conversation model failed")
    if isinstance(error, ConversationCoordinatorError):
        detail = str(error)
        if detail == "permission denied":
            return HTTPException(403, "permission denied")
        if detail in {"conversation unavailable", "user message unavailable"}:
            return HTTPException(404, "conversation or user message not found")
        if detail == "completed result unavailable":
            return HTTPException(503, "conversation result unavailable")
        return HTTPException(409, "conversation turn unavailable")
    return HTTPException(503, "conversation execution unavailable")


def register_conversation_turn_routes(app, write_event):
    async def writer(
        principal: Principal = Depends(require_permission("conversations:write")),
    ):
        if "conversations:read" not in principal.permissions:
            raise HTTPException(403, "permission denied")
        return principal

    async def owned_turn(conversation_id, turn_id, principal):
        async with app.state.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT turn.*
                FROM conversation_turns AS turn
                JOIN conversations AS conversation
                  ON conversation.tenant_id = turn.tenant_id
                 AND conversation.conversation_id = turn.conversation_id
                WHERE turn.turn_id = $1 AND turn.conversation_id = $2
                  AND turn.tenant_id = $3
                  AND conversation.owner_subject_id = $4
                """,
                turn_id, conversation_id, principal.tenant_id, principal.subject_id,
            )
        if row is None:
            raise HTTPException(404, "conversation turn not found")
        return dict(row)

    @app.post('/conversations/{conversation_id}/turns')
    async def create_conversation_turn(conversation_id: UUID, request: ConversationTurnCreate, principal: Principal=Depends(writer)):
        try:
            tool_mode = configured_tool_proposals()
            if tool_mode and 'jobs:create' not in principal.permissions:
                raise HTTPException(403, 'permission denied')
            if tool_mode and (not callable(getattr(app.state.conversation_model, 'complete_with_tools', None))):
                raise HTTPException(503, 'structured conversation model unavailable')
            async with app.state.pool.acquire() as connection:
                source = await connection.fetchrow('\n                    SELECT message.role\n                    FROM conversation_messages AS message\n                    JOIN conversations AS conversation\n                      ON conversation.tenant_id = message.tenant_id\n                     AND conversation.conversation_id = message.conversation_id\n                    WHERE message.message_id = $1\n                      AND message.conversation_id = $2\n                      AND message.tenant_id = $3\n                      AND conversation.owner_subject_id = $4\n                    ', request.user_message_id, conversation_id, principal.tenant_id, principal.subject_id)
            if source is None:
                raise HTTPException(404, 'conversation or user message not found')
            if source['role'] != 'user':
                raise HTTPException(409, 'turn source must be a user message')
            await expire_conversation_turns(app.state.pool, principal, conversation_id, write_event=write_event)
            workspace_options = {}
            if 'workspace_project_id' in request.model_fields_set:
                workspace_options['workspace_project_id'] = request.workspace_project_id
            if tool_mode:
                workspace_options['tool_mode'] = True
            result = await execute_turn(app.state.pool, principal, conversation_id, request.user_message_id, model=app.state.conversation_model, write_event=write_event, memory_context=request.memory_context, **workspace_options)
            return {'turn': public_turn(result['turn']), 'message': jsonable_encoder(result['message'])}
        except HTTPException:
            raise
        except Exception as error:
            raise turn_http_error(error) from error

    @app.get("/conversations/{conversation_id}/turns/{turn_id}")
    async def get_conversation_turn(
        conversation_id: UUID,
        turn_id: UUID,
        principal: Principal = Depends(require_permission("conversations:read")),
    ):
        try:
            await owned_turn(conversation_id, turn_id, principal)
            await expire_conversation_turns(
                app.state.pool, principal, conversation_id, write_event=write_event,
            )
            return public_turn(await owned_turn(conversation_id, turn_id, principal))
        except HTTPException:
            raise
        except Exception as error:
            raise turn_http_error(error) from error

    @app.post("/conversations/{conversation_id}/turns/{turn_id}/cancel")
    async def cancel_conversation_turn(
        conversation_id: UUID,
        turn_id: UUID,
        principal: Principal = Depends(writer),
    ):
        try:
            await owned_turn(conversation_id, turn_id, principal)
            await expire_conversation_turns(
                app.state.pool, principal, conversation_id, write_event=write_event,
            )
            row = await owned_turn(conversation_id, turn_id, principal)
            if row["status"] == "cancelled":
                return public_turn(row)
            if row["status"] in {"completed", "failed"}:
                raise HTTPException(409, "turn is terminal")
            try:
                result = await cancel_turn(
                    app.state.pool, principal, turn_id, write_event=write_event,
                )
            except ConversationCoordinatorError:
                row = await owned_turn(conversation_id, turn_id, principal)
                if row["status"] == "cancelled":
                    return public_turn(row)
                raise
            return public_turn(result)
        except HTTPException:
            raise
        except Exception as error:
            raise turn_http_error(error) from error
