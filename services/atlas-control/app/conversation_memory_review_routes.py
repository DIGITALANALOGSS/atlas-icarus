from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, model_validator

from app.auth import Principal, require_permission


class MemoryProposalReview(BaseModel):
    model_config = {"extra": "forbid"}

    decision: Literal["accepted", "rejected"]
    memory_scope: Literal["conversation", "personal", "project"] = "conversation"
    workspace_project_id: UUID | None = None

    @model_validator(mode="after")
    def validate_review_scope(self):
        if self.decision == "rejected" and self.memory_scope != "conversation":
            raise ValueError("rejection cannot assign a broader memory scope")
        if self.memory_scope == "project":
            if self.workspace_project_id is None:
                raise ValueError("project scope requires workspace_project_id")
        elif self.workspace_project_id is not None:
            raise ValueError("workspace_project_id requires project scope")
        return self


_PROPOSAL_COLUMNS = """
    p.proposal_id, p.conversation_id, p.source_message_id, p.job_id,
    p.category, p.content, p.status, p.created_at,
    p.reviewed_at, p.reviewed_by_subject_id,
    p.memory_scope, p.workspace_project_id, p.lifecycle_state
"""


async def _owned_conversation(connection, conversation_id, principal, *, lock):
    if lock not in {"SHARE", "UPDATE"}:
        raise ValueError("unsupported conversation lock")
    row = await connection.fetchrow(
        """
        SELECT conversation_id, correlation_id
        FROM conversations
        WHERE conversation_id = $1
          AND tenant_id = $2
          AND owner_subject_id = $3
        FOR """ + lock,
        conversation_id,
        principal.tenant_id,
        principal.subject_id,
    )
    if row is None:
        raise HTTPException(404, "conversation not found")
    return row


def register_conversation_memory_review_routes(app, write_event):
    async def writer(
        principal: Principal = Depends(require_permission("conversations:write")),
    ):
        if "conversations:read" not in principal.permissions:
            raise HTTPException(403, "permission denied")
        return principal

    @app.get("/conversations/{conversation_id}/memory-proposals")
    async def list_memory_proposals(
        conversation_id: UUID,
        proposal_status: Literal["proposed", "accepted", "rejected"] = Query(
            default="proposed", alias="status"
        ),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        principal: Principal = Depends(require_permission("conversations:read")),
    ) -> dict:
        try:
            async with app.state.pool.acquire() as connection:
                async with connection.transaction():
                    await _owned_conversation(
                        connection, conversation_id, principal, lock="SHARE"
                    )
                    rows = await connection.fetch(
                        "SELECT " + _PROPOSAL_COLUMNS + """
                        FROM conversation_memory_proposals AS p
                        WHERE p.conversation_id = $1
                          AND p.tenant_id = $2
                          AND p.status = $3
                        ORDER BY p.created_at, p.proposal_id
                        LIMIT $4 OFFSET $5
                        """,
                        conversation_id,
                        principal.tenant_id,
                        proposal_status,
                        limit,
                        offset,
                    )
            return {
                "items": [jsonable_encoder(dict(row)) for row in rows],
                "limit": limit,
                "offset": offset,
                "status": proposal_status,
            }
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "database query failed") from exc

    @app.post(
        "/conversations/{conversation_id}/memory-proposals/{proposal_id}/review"
    )
    async def review_memory_proposal(
        conversation_id: UUID,
        proposal_id: UUID,
        review: MemoryProposalReview,
        principal: Principal = Depends(writer),
    ) -> dict:
        try:
            async with app.state.pool.acquire() as connection:
                async with connection.transaction():
                    conversation = await _owned_conversation(
                        connection, conversation_id, principal, lock="UPDATE"
                    )
                    proposal = await connection.fetchrow(
                        "SELECT " + _PROPOSAL_COLUMNS + """
                        FROM conversation_memory_proposals AS p
                        WHERE p.proposal_id = $1
                          AND p.conversation_id = $2
                          AND p.tenant_id = $3
                        FOR UPDATE OF p
                        """,
                        proposal_id,
                        conversation_id,
                        principal.tenant_id,
                    )
                    if proposal is None:
                        raise HTTPException(404, "memory proposal not found")

                    if proposal["status"] != "proposed":
                        if (
                            proposal["status"] != review.decision
                            or proposal["memory_scope"] != review.memory_scope
                            or proposal["workspace_project_id"] != review.workspace_project_id
                        ):
                            raise HTTPException(
                                409, "memory proposal already reviewed with a different decision or scope"
                            )
                        result = dict(proposal)
                    else:
                        if review.memory_scope == "project":
                            binding = await connection.fetchrow(
                                """
                                SELECT workspace_project_id
                                FROM conversation_workspace_bindings
                                WHERE tenant_id = $1
                                  AND conversation_id = $2
                                  AND scope_type = 'project'
                                  AND workspace_project_id = $3
                                """,
                                principal.tenant_id,
                                conversation_id,
                                review.workspace_project_id,
                            )
                            if binding is None:
                                raise HTTPException(
                                    409,
                                    "project scope does not match conversation binding",
                                )
                        updated = await connection.fetchrow(
                            """
                            UPDATE conversation_memory_proposals
                            SET status = $4,
                                reviewed_at = NOW(),
                                reviewed_by_subject_id = $5,
                                memory_scope = $6,
                                workspace_project_id = $7
                            WHERE proposal_id = $1
                              AND conversation_id = $2
                              AND tenant_id = $3
                              AND status = 'proposed'
                            RETURNING
                                proposal_id, conversation_id, source_message_id,
                                job_id, category, content, status, created_at,
                                reviewed_at, reviewed_by_subject_id,
                                memory_scope, workspace_project_id
                            """,
                            proposal_id,
                            conversation_id,
                            principal.tenant_id,
                            review.decision,
                            principal.subject_id,
                            review.memory_scope,
                            review.workspace_project_id,
                        )
                        if updated is None:
                            raise HTTPException(
                                409, "memory proposal changed during review"
                            )
                        await write_event(
                            connection,
                            tenant_id=principal.tenant_id,
                            event_type="governance.conversation.memory_proposal.reviewed",
                            correlation_id=(
                                conversation["correlation_id"] or uuid4()
                            ),
                            payload={
                                "conversation_id": str(conversation_id),
                                "proposal_id": str(proposal_id),
                                "source_message_id": str(
                                    proposal["source_message_id"]
                                ),
                                "job_id": str(proposal["job_id"]),
                                "decision": review.decision,
                                "memory_scope": review.memory_scope,
                                "workspace_project_id": (
                                    str(review.workspace_project_id)
                                    if review.workspace_project_id is not None
                                    else None
                                ),
                                "reviewed_by_subject_id": principal.subject_id,
                            },
                        )
                        result = dict(updated)
            return jsonable_encoder(result)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "database write failed") from exc

    from app.conversation_memory_lifecycle_routes import (
        register_memory_lifecycle_routes,
    )
    register_memory_lifecycle_routes(app, write_event)
