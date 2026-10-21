from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field, field_validator

from app.auth import Principal, require_permission
from app.conversation_memory_lifecycle import (
    MemoryLifecycleError, transition_memory,
)


class MemoryWithdrawal(BaseModel):
    model_config = {"extra": "forbid"}
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value):
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


class MemorySupersession(MemoryWithdrawal):
    replacement_proposal_id: UUID


def register_memory_lifecycle_routes(app, write_event):
    async def writer(
        principal: Principal = Depends(require_permission("conversations:write")),
    ):
        if "conversations:read" not in principal.permissions:
            raise HTTPException(403, "permission denied")
        return principal

    async def apply(principal, proposal_id, request, replacement=None):
        try:
            result = await transition_memory(
                app.state.pool, principal, proposal_id,
                reason=request.reason, write_event=write_event,
                replacement_proposal_id=replacement,
            )
            return jsonable_encoder(result)
        except MemoryLifecycleError as exc:
            detail = str(exc)
            code = 404 if detail == "memory unavailable" else 409
            if detail == "permission denied":
                code = 403
            raise HTTPException(code, detail) from exc
        except Exception as exc:
            raise HTTPException(503, "memory lifecycle update failed") from exc

    @app.post("/memory-proposals/{proposal_id}/revoke")
    async def revoke(
        proposal_id: UUID, request: MemoryWithdrawal,
        principal: Principal = Depends(writer),
    ):
        return await apply(principal, proposal_id, request)

    @app.post("/memory-proposals/{proposal_id}/supersede")
    async def supersede(
        proposal_id: UUID, request: MemorySupersession,
        principal: Principal = Depends(writer),
    ):
        return await apply(
            principal, proposal_id, request, request.replacement_proposal_id,
        )
