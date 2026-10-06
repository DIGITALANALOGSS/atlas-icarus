"""Typed adapters for the local single-job workflow path."""

import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid5

from .workflows import (
    WorkflowNodeRequest,
    WorkflowNodeResult,
    WorkflowStatus,
)


def actor_id_for_subject(tenant_id: UUID, subject_id: str) -> UUID:
    """Derive a tenant-scoped identity, not a registered actor record."""
    if not isinstance(subject_id, str) or not subject_id.strip():
        raise ValueError("subject_id must be a nonblank string")
    name = json.dumps(
        ["atlas.actor.v1", subject_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return uuid5(tenant_id, name)


def execute_text_analysis(
    request: WorkflowNodeRequest,
    *,
    completed_at: datetime,
    analyzed_at: str,
) -> WorkflowNodeResult:
    """Execute metadata.analyze without database or authentication access."""
    if request.node_type != "metadata.analyze":
        raise ValueError("unsupported node type for text-analysis adapter")

    content = request.input.get("content")
    if not isinstance(content, str):
        raise ValueError("text-analysis input.content must be a string")

    output = {
        "char_count": len(content),
        "word_count": len(content.split()),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "analyzed_at": analyzed_at,
    }

    return WorkflowNodeResult(
        node_id=request.node_id,
        workflow_id=request.workflow_id,
        correlation_id=request.correlation_id,
        causation_id=request.causation_id,
        tenant_id=request.tenant_id,
        status=WorkflowStatus.SUCCEEDED,
        output=output,
        completed_at=completed_at,
    )
