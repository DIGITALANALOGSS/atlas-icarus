from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


WORKFLOW_SCHEMA_VERSION = "v1"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def strip_nonblank(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


class WorkflowStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SourceReference(BaseModel):
    source_id: UUID = Field(default_factory=uuid4)
    source_type: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=500)
    locator: str = Field(min_length=1, max_length=2000)
    excerpt: str | None = Field(default=None, max_length=5000)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_type", "title", "locator")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        return strip_nonblank(value)


class WorkflowRequest(BaseModel):
    workflow_id: UUID = Field(default_factory=uuid4)
    correlation_id: UUID = Field(default_factory=uuid4)
    causation_id: UUID | None = None
    tenant_id: UUID
    actor_id: UUID
    job_id: UUID | None = None
    approval_gate_id: UUID | None = None
    schema_version: str = WORKFLOW_SCHEMA_VERSION
    created_at: datetime = Field(default_factory=utc_now)
    input_text: str = Field(min_length=1, max_length=20_000)
    document_references: list[UUID] = Field(default_factory=list, max_length=100)
    image_references: list[UUID] = Field(default_factory=list, max_length=100)
    requested_capabilities: list[str] = Field(default_factory=list, max_length=20)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("input_text")
    @classmethod
    def normalize_input_text(cls, value: str) -> str:
        return strip_nonblank(value)

    @field_validator("requested_capabilities")
    @classmethod
    def normalize_capabilities(cls, value: list[str]) -> list[str]:
        normalized = [strip_nonblank(item).lower() for item in value]
        if len(set(normalized)) != len(normalized):
            raise ValueError("must not contain duplicates")
        return normalized


class WorkflowNodeRequest(BaseModel):
    node_id: UUID = Field(default_factory=uuid4)
    workflow_id: UUID
    correlation_id: UUID
    causation_id: UUID | None = None
    tenant_id: UUID
    actor_id: UUID
    node_type: str = Field(min_length=1, max_length=100)
    attempt: int = Field(default=1, ge=1, le=100)
    input: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    @field_validator("node_type")
    @classmethod
    def normalize_node_type(cls, value: str) -> str:
        return strip_nonblank(value).lower()


class WorkflowError(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=2000)
    retryable: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("code", "message")
    @classmethod
    def normalize_error_text(cls, value: str) -> str:
        return strip_nonblank(value)


class WorkflowNodeResult(BaseModel):
    result_id: UUID = Field(default_factory=uuid4)
    node_id: UUID
    workflow_id: UUID
    correlation_id: UUID
    causation_id: UUID | None = None
    tenant_id: UUID
    status: WorkflowStatus
    output: dict[str, Any] = Field(default_factory=dict)
    sources: list[SourceReference] = Field(default_factory=list, max_length=100)
    error: WorkflowError | None = None
    completed_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_error_for_status(self) -> "WorkflowNodeResult":
        if self.status == WorkflowStatus.FAILED and self.error is None:
            raise ValueError("error is required when status is failed")
        if self.status != WorkflowStatus.FAILED and self.error is not None:
            raise ValueError("error is only allowed when status is failed")
        return self
