import hashlib
from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.workflow_adapters import (
    actor_id_for_subject,
    execute_text_analysis,
)
from app.workflows import WorkflowNodeRequest, WorkflowStatus


TENANT = UUID("11111111-1111-4111-8111-111111111111")
OTHER_TENANT = UUID("22222222-2222-4222-8222-222222222222")
JOB = UUID("33333333-3333-4333-8333-333333333333")
CORRELATION = UUID("44444444-4444-4444-8444-444444444444")
COMPLETED = datetime(2026, 10, 6, 23, 0, tzinfo=timezone.utc)


def make_request(content, node_type="metadata.analyze"):
    return WorkflowNodeRequest(
        node_id=JOB,
        workflow_id=JOB,
        correlation_id=CORRELATION,
        tenant_id=TENANT,
        actor_id=actor_id_for_subject(TENANT, "atlas-admin"),
        node_type=node_type,
        input={"content": content},
    )


def test_actor_mapping_is_stable_and_tenant_scoped():
    actor = actor_id_for_subject(TENANT, "atlas-admin")
    assert isinstance(actor, UUID)
    assert actor == actor_id_for_subject(TENANT, "atlas-admin")
    assert actor != actor_id_for_subject(OTHER_TENANT, "atlas-admin")
    assert actor != actor_id_for_subject(TENANT, "atlas-job-worker")


@pytest.mark.parametrize("subject", ["", " ", None])
def test_actor_mapping_rejects_invalid_subject(subject):
    with pytest.raises(ValueError):
        actor_id_for_subject(TENANT, subject)


@pytest.mark.parametrize(
    "content", ["Hello world", "", "  hello\tworld\n", "café 🌍"]
)
def test_adapter_preserves_output_and_identity(content):
    request = make_request(content)
    timestamp = COMPLETED.isoformat()
    result = execute_text_analysis(
        request, completed_at=COMPLETED, analyzed_at=timestamp
    )
    assert result.status == WorkflowStatus.SUCCEEDED
    assert result.node_id == request.node_id
    assert result.workflow_id == request.workflow_id
    assert result.correlation_id == request.correlation_id
    assert result.tenant_id == request.tenant_id
    assert result.causation_id == request.causation_id
    assert result.completed_at == COMPLETED
    assert result.error is None
    assert result.output == {
        "char_count": len(content),
        "word_count": len(content.split()),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "analyzed_at": timestamp,
    }


def test_adapter_rejects_unsupported_node_type():
    with pytest.raises(ValueError, match="unsupported node type"):
        execute_text_analysis(
            make_request("hello", "unsupported"),
            completed_at=COMPLETED,
            analyzed_at=COMPLETED.isoformat(),
        )


@pytest.mark.parametrize("content", [None, 123, {}])
def test_adapter_rejects_nonstring_content(content):
    with pytest.raises(ValueError, match="must be a string"):
        execute_text_analysis(
            make_request(content),
            completed_at=COMPLETED,
            analyzed_at=COMPLETED.isoformat(),
        )
