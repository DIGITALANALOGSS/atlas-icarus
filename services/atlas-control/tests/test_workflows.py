from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

APP_ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(APP_ROOT))

from app.workflows import (
    WORKFLOW_SCHEMA_VERSION,
    SourceReference,
    WorkflowError,
    WorkflowNodeRequest,
    WorkflowNodeResult,
    WorkflowRequest,
    WorkflowStatus,
)


TENANT_ID = uuid4()
ACTOR_ID = uuid4()
WORKFLOW_ID = uuid4()
CORRELATION_ID = uuid4()
NODE_ID = uuid4()


def test_workflow_request_generates_ids_and_normalizes_fields():
    request = WorkflowRequest(
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        input_text="  Summarize the attached report.  ",
        requested_capabilities=[" Research ", "retrieval"],
    )

    assert request.workflow_id
    assert request.correlation_id
    assert request.schema_version == WORKFLOW_SCHEMA_VERSION
    assert request.input_text == "Summarize the attached report."
    assert request.requested_capabilities == ["research", "retrieval"]
    assert request.created_at.tzinfo == timezone.utc


def test_workflow_request_rejects_blank_input_and_duplicate_capabilities():
    with pytest.raises(ValidationError):
        WorkflowRequest(
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            input_text="   ",
        )

    with pytest.raises(ValidationError):
        WorkflowRequest(
            tenant_id=TENANT_ID,
            actor_id=ACTOR_ID,
            input_text="Analyze this.",
            requested_capabilities=["research", " Research "],
        )


def test_source_reference_normalizes_and_validates_sha256():
    source = SourceReference(
        source_type="  document ",
        title="  Atlas Plan ",
        locator="  object://atlas/plan.pdf ",
        sha256="a" * 64,
    )

    assert source.source_type == "document"
    assert source.title == "Atlas Plan"
    assert source.locator == "object://atlas/plan.pdf"

    with pytest.raises(ValidationError):
        SourceReference(
            source_type="document",
            title="Bad hash",
            locator="object://atlas/bad.pdf",
            sha256="not-a-sha256",
        )


def test_node_request_carries_workflow_identity_and_normalizes_type():
    request = WorkflowNodeRequest(
        workflow_id=WORKFLOW_ID,
        correlation_id=CORRELATION_ID,
        tenant_id=TENANT_ID,
        actor_id=ACTOR_ID,
        node_type="  Retrieval ",
        input={"query": "Find the source material."},
    )

    assert request.node_type == "retrieval"
    assert request.attempt == 1
    assert request.workflow_id == WORKFLOW_ID
    assert request.correlation_id == CORRELATION_ID


def test_failed_node_result_requires_error_and_success_rejects_error():
    error = WorkflowError(
        code="retrieval_unavailable",
        message="Retriever timed out.",
        retryable=True,
    )

    failed = WorkflowNodeResult(
        node_id=NODE_ID,
        workflow_id=WORKFLOW_ID,
        correlation_id=CORRELATION_ID,
        tenant_id=TENANT_ID,
        status=WorkflowStatus.FAILED,
        error=error,
    )

    assert failed.error == error

    with pytest.raises(ValidationError):
        WorkflowNodeResult(
            node_id=NODE_ID,
            workflow_id=WORKFLOW_ID,
            correlation_id=CORRELATION_ID,
            tenant_id=TENANT_ID,
            status=WorkflowStatus.FAILED,
        )

    with pytest.raises(ValidationError):
        WorkflowNodeResult(
            node_id=NODE_ID,
            workflow_id=WORKFLOW_ID,
            correlation_id=CORRELATION_ID,
            tenant_id=TENANT_ID,
            status=WorkflowStatus.SUCCEEDED,
            error=error,
        )


def test_node_result_preserves_source_provenance():
    result = WorkflowNodeResult(
        node_id=NODE_ID,
        workflow_id=WORKFLOW_ID,
        correlation_id=CORRELATION_ID,
        tenant_id=TENANT_ID,
        status=WorkflowStatus.SUCCEEDED,
        output={"answer": "The source supports the recommendation."},
        sources=[
            SourceReference(
                source_type="pdf",
                title="Research Notes",
                locator="object://atlas/research-notes.pdf",
                excerpt="The recommended architecture uses governed workers.",
            )
        ],
    )

    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.sources[0].title == "Research Notes"
    assert result.error is None
