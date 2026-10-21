
"""atlas-voice-stream-routes-v1: provisional token events and explicit cancellation."""
import asyncio
import json
import logging
from contextlib import aclosing
from uuid import UUID

import anyio
from fastapi import Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.auth import Principal, require_permission
from app.conversation_adapters import ConversationTurn
from app.conversation_memory import memory_system_message, stored_memory_context
from app.conversation_coordinator import (
    ConversationCoordinatorError, reserve_turn, claim_turn,
    _load_turn_context, publish_turn, cancel_turn, fail_turn, execute_turn,
)

log = logging.getLogger(__name__)
ACTIVE = {}

class VoiceTurnRequest(BaseModel):
    model_config = {"extra": "forbid"}
    user_message_id: UUID
    workspace_project_id: UUID | None = None
    memory_context: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("memory_context")
    @classmethod
    def valid_memory(cls, value):
        memory_system_message(value)
        return value

def event(kind, **fields):
    return (
        json.dumps(
            jsonable_encoder({"type": kind, **fields}),
            ensure_ascii=False,
        ) + "\n"
    ).encode("utf-8")

def key(principal, turn_id):
    return (
        str(principal.tenant_id),
        principal.subject_id,
        str(turn_id),
    )

def register_voice_stream_routes(app, write_event):
    async def writer(
        principal: Principal = Depends(
            require_permission("conversations:write")
        ),
    ):
        if "conversations:read" not in principal.permissions:
            raise HTTPException(403, "permission denied")
        return principal

    @app.post("/conversations/{conversation_id}/voice-turns")
    async def voice_turn(
        conversation_id: UUID,
        body: VoiceTurnRequest,
        request: Request,
        principal: Principal = Depends(writer),
    ):
        model = app.state.conversation_model
        if not callable(getattr(model, "stream", None)):
            raise HTTPException(503, "configured model has no streaming adapter")

        options = {"memory_context": body.memory_context}
        if "workspace_project_id" in body.model_fields_set:
            options["workspace_project_id"] = body.workspace_project_id

        try:
            reserved = await reserve_turn(
                app.state.pool, principal, conversation_id,
                body.user_message_id, write_event=write_event, **options,
            )
        except ConversationCoordinatorError as exc:
            raise HTTPException(409, str(exc)) from exc

        if reserved["status"] == "completed":
            result = await execute_turn(
                app.state.pool, principal, conversation_id,
                body.user_message_id, model=model,
                write_event=write_event, **options,
            )
            async def replay():
                yield event("completed", result=result, replay=True)
            return StreamingResponse(
                replay(), media_type="application/x-ndjson",
                headers={"Cache-Control": "no-store"},
            )

        if reserved["status"] != "queued":
            raise HTTPException(409, "turn is already running or terminal")

        async def generate():
            turn_id = reserved["turn_id"]
            identity = key(principal, turn_id)
            token = None
            terminal = False
            active_task = asyncio.current_task()
            try:
                claimed = await claim_turn(
                    app.state.pool, principal, turn_id,
                    write_event=write_event,
                )
                token = claimed["worker_token"]
                ACTIVE[identity] = active_task
                rows, remaining = await _load_turn_context(
                    app.state.pool, principal, turn_id, token,
                    expected_memory_sha256=reserved["memory_context_sha256"],
                )
                context = [
                    ConversationTurn(row["role"], row["content"])
                    for row in rows
                ]
                memory = memory_system_message(
                    stored_memory_context(reserved)
                )
                if memory is not None:
                    context.insert(0, ConversationTurn("system", memory))

                yield event("started", turn_id=turn_id)
                fragments = []
                characters = 0

                async with asyncio.timeout(remaining):
                    async with aclosing(model.stream(context)) as stream:
                        async for fragment in stream:
                            if await request.is_disconnected():
                                return
                            if not isinstance(fragment, str):
                                raise ValueError("invalid model text fragment")
                            characters += len(fragment)
                            if characters > 200000:
                                raise ValueError("voice reply limit exceeded")
                            fragments.append(fragment)
                            yield event(
                                "delta", turn_id=turn_id,
                                text=fragment, provisional=True,
                            )

                content = "".join(fragments).strip()
                if not content:
                    raise ValueError("empty streamed response")
                if await request.is_disconnected():
                    return

                result = await publish_turn(
                    app.state.pool, principal, turn_id,
                    worker_token=token, content=content,
                    write_event=write_event,
                )
                terminal = True
                yield event("completed", result=result, replay=False)

            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Voice turn stream failed: %s", turn_id)
                if token is not None:
                    with anyio.move_on_after(5, shield=True):
                        try:
                            await fail_turn(
                                app.state.pool, principal, turn_id,
                                worker_token=token,
                                error_code="voice_stream_failed",
                                write_event=write_event,
                            )
                            terminal = True
                        except Exception:
                            log.exception(
                                "Voice failure finalization failed: %s",
                                turn_id,
                            )
                if not await request.is_disconnected():
                    yield event(
                        "error", turn_id=turn_id,
                        detail="Stream failed. Partial text is not a completed answer.",
                    )
            finally:
                if ACTIVE.get(identity) is active_task:
                    ACTIVE.pop(identity, None)
                if token is not None and not terminal:
                    with anyio.move_on_after(5, shield=True):
                        try:
                            await cancel_turn(
                                app.state.pool, principal, turn_id,
                                write_event=write_event,
                            )
                        except Exception:
                            log.exception(
                                "Voice cancellation not confirmed: %s",
                                turn_id,
                            )

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/conversations/{conversation_id}/voice-turns/{turn_id}/cancel"
    )
    async def cancel_voice(
        conversation_id: UUID,
        turn_id: UUID,
        principal: Principal = Depends(writer),
    ):
        async with app.state.pool.acquire() as connection:
            owned = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM conversation_turns t
                    JOIN conversations c
                      ON c.tenant_id=t.tenant_id
                     AND c.conversation_id=t.conversation_id
                    WHERE t.turn_id=$1 AND t.tenant_id=$2
                      AND t.conversation_id=$3
                      AND c.owner_subject_id=$4
                )
                """,
                turn_id, principal.tenant_id,
                conversation_id, principal.subject_id,
            )
        if not owned:
            raise HTTPException(404, "turn unavailable")
        try:
            result = await cancel_turn(
                app.state.pool, principal, turn_id,
                write_event=write_event,
            )
        except ConversationCoordinatorError as exc:
            raise HTTPException(409, str(exc)) from exc

        task = ACTIVE.get(key(principal, turn_id))
        if task is not None and not task.done():
            task.cancel()
        return jsonable_encoder(result)
