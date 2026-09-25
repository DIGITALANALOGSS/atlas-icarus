import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Query, status
from app.auth import Principal, require_permission
from pydantic import BaseModel, Field, field_validator

SERVICE_NAME = "atlas-control"
SERVICE_VERSION = "0.1.0"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
ACTION_TYPE_PATTERN = re.compile(r"^[a-z0-9]+(\.[a-z0-9]+)+$")
RISK_LEVELS = {"low", "medium", "high", "critical"}
APPROVAL_STATUSES = {"pending", "approved", "rejected"}


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("received_at must include a UTC offset or Z")
    return value.astimezone(timezone.utc)


def strip_nonblank(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


def decode_json_object(value: dict | str) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("database JSON value must be an object")
    return value


class IntakeItemCreate(BaseModel):
    source: str = Field(min_length=1, max_length=1000)
    received_at: datetime
    custodian: str = Field(min_length=1, max_length=1000)
    storage_reference: str = Field(min_length=1, max_length=2000)
    original_sha256: str
    notes: str | None = Field(default=None, max_length=2000)
    correlation_id: UUID | None = None

    @field_validator("source", "custodian", "storage_reference")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return strip_nonblank(value)

    @field_validator("received_at")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @field_validator("original_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("must be exactly 64 lowercase hexadecimal characters")
        return value

    @field_validator("notes")
    @classmethod
    def strip_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class EvidenceRecordCreate(BaseModel):
    sha256: str
    storage_reference: str = Field(min_length=1, max_length=2000)
    media_type: str | None = Field(default=None, max_length=255)
    filename: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=2000)
    metadata: dict = Field(default_factory=dict)

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not SHA256_PATTERN.fullmatch(value):
            raise ValueError("must be exactly 64 lowercase hexadecimal characters")
        return value

    @field_validator("storage_reference")
    @classmethod
    def strip_storage_reference(cls, value: str) -> str:
        return strip_nonblank(value)

    @field_validator("media_type", "filename", "description")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


class ApprovalGateCreate(BaseModel):
    requester: str = Field(min_length=1, max_length=1000)
    action_type: str = Field(min_length=3, max_length=255)
    risk_level: str
    action_payload: dict = Field(default_factory=dict)
    summary: str = Field(min_length=1, max_length=2000)
    correlation_id: UUID | None = None

    @field_validator("requester", "summary")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        return strip_nonblank(value)

    @field_validator("action_type")
    @classmethod
    def validate_action_type(cls, value: str) -> str:
        value = value.strip()
        if not ACTION_TYPE_PATTERN.fullmatch(value):
            raise ValueError("must be a lowercase dotted identifier")
        return value

    @field_validator("risk_level")
    @classmethod
    def validate_risk_level(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in RISK_LEVELS:
            raise ValueError("must be one of: low, medium, high, critical")
        return value


class ApprovalDecisionCreate(BaseModel):
    decided_by: str = Field(min_length=1, max_length=1000)
    decision_reason: str | None = Field(default=None, max_length=2000)

    @field_validator("decided_by")
    @classmethod
    def strip_decided_by(cls, value: str) -> str:
        return strip_nonblank(value)

    @field_validator("decision_reason")
    @classmethod
    def strip_decision_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None


JOB_TYPE_PATTERN = re.compile(r"^[a-z0-9]+(\.[a-z0-9_]+)+$")
JOB_STATUSES = {
    "pending_approval",
    "queued",
    "running",
    "succeeded",
    "failed",
    "rejected",
}


class JobCreate(BaseModel):
    job_type: str = Field(min_length=3, max_length=255)
    request_payload: dict = Field(default_factory=dict)
    approval_required: bool = False
    correlation_id: UUID | None = None

    @field_validator("job_type")
    @classmethod
    def validate_job_type(cls, value: str) -> str:
        value = value.strip()
        if not JOB_TYPE_PATTERN.fullmatch(value):
            raise ValueError("must be a lowercase dotted identifier")
        if value != "metadata.analyze":
            raise ValueError("only metadata.analyze is supported")
        return value

    @field_validator("request_payload")
    @classmethod
    def validate_request_payload(cls, value: dict) -> dict:
        content = value.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("must include nonblank string field: content")
        if len(content) > 20000:
            raise ValueError("content must not exceed 20000 characters")
        return value


async def apply_migrations(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as connection:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version TEXT PRIMARY KEY,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.name
            already_applied = await connection.fetchval(
                "SELECT 1 FROM schema_migrations WHERE version = $1",
                version,
            )
            if already_applied:
                continue

            sql = path.read_text(encoding="utf-8")
            async with connection.transaction():
                await connection.execute(sql)
                await connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)",
                    version,
                )


@asynccontextmanager
async def lifespan(app: FastAPI):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")

    pool = await asyncpg.create_pool(
        database_url,
        min_size=1,
        max_size=5,
        command_timeout=5,
    )
    try:
        await apply_migrations(pool)
        app.state.pool = pool
        yield
    finally:
        await pool.close()


app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION, lifespan=lifespan)


def serialize_evidence_record(row) -> dict:
    return {
        "evidence_id": str(row["evidence_id"]),
        "intake_id": str(row["intake_id"]),
        "sha256": row["sha256"],
        "storage_reference": row["storage_reference"],
        "media_type": row["media_type"],
        "filename": row["filename"],
        "description": row["description"],
        "metadata": decode_json_object(row["metadata"]),
        "correlation_id": str(row["correlation_id"]),
        "created_at": serialize_datetime(row["created_at"]),
    }


def serialize_approval_gate(row) -> dict:
    return {
        "gate_id": str(row["gate_id"]),
        "requester": row["requester"],
        "action_type": row["action_type"],
        "risk_level": row["risk_level"],
        "action_payload": decode_json_object(row["action_payload"]),
        "summary": row["summary"],
        "status": row["status"],
        "correlation_id": str(row["correlation_id"]),
        "created_at": serialize_datetime(row["created_at"]),
        "decided_at": serialize_datetime(row["decided_at"]),
        "decided_by": row["decided_by"],
        "decision_reason": row["decision_reason"],
        "decision_event_id": (
            str(row["decision_event_id"])
            if row["decision_event_id"] is not None
            else None
        ),
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "timestamp_utc": timestamp_utc(),
    }


@app.get("/readyz")
async def readyz():
    pool = getattr(app.state, "pool", None)
    if pool is None:
        return {
            "status": "not_ready",
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "timestamp_utc": timestamp_utc(),
            "reason": "database pool is not initialized",
        }

    try:
        async with pool.acquire() as connection:
            await connection.execute("SELECT 1")
    except Exception:
        return {
            "status": "not_ready",
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "timestamp_utc": timestamp_utc(),
            "reason": "database connection failed",
        }

    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "timestamp_utc": timestamp_utc(),
        "database": "reachable",
    }


@app.post("/intake-items", status_code=status.HTTP_201_CREATED)
async def create_intake_item(
    item: IntakeItemCreate,
    principal: Principal = Depends(require_permission("research:intake:create")),
) -> dict:
    intake_id = uuid4()
    event_id = uuid4()
    correlation_id = item.correlation_id or uuid4()
    occurred_at = datetime.now(timezone.utc)
    payload = {
        "intake_id": str(intake_id),
        "source": item.source,
        "received_at": serialize_datetime(item.received_at),
        "custodian": item.custodian,
        "storage_reference": item.storage_reference,
        "original_sha256": item.original_sha256,
        "notes": item.notes,
    }

    try:
        async with app.state.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO intake_items (
                      intake_id, tenant_id, source, received_at, custodian,
                      storage_reference, original_sha256, notes, correlation_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    intake_id,
                    principal.tenant_id,
                    item.source,
                    item.received_at,
                    item.custodian,
                    item.storage_reference,
                    item.original_sha256,
                    item.notes,
                    correlation_id,
                )
                await connection.execute(
                    """
                    INSERT INTO events (
                      event_id, event_type, occurred_at, producer,
                      correlation_id, schema_version, payload
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    """,
                    event_id,
                    "research.intake.created",
                    occurred_at,
                    SERVICE_NAME,
                    correlation_id,
                    "1.0",
                    json.dumps(payload),
                )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database write failed",
        ) from exc

    return {
        "intake_id": str(intake_id),
        "event_id": str(event_id),
        "event_type": "research.intake.created",
        "correlation_id": str(correlation_id),
        "occurred_at": serialize_datetime(occurred_at),
        "item": payload,
    }


@app.get("/evidence-records/{evidence_id}")
async def get_evidence_record(
    evidence_id: UUID,
    principal: Principal = Depends(require_permission("research:evidence:read")),
) -> dict:
    try:
        async with app.state.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                  evidence_id, intake_id, sha256, storage_reference,
                  media_type, filename, description, metadata,
                  correlation_id, created_at
                FROM evidence_records
                WHERE evidence_id = $1
                  AND tenant_id = $2
                """,
                evidence_id,
                principal.tenant_id,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database query failed",
        ) from exc

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="evidence record not found",
        )

    return serialize_evidence_record(row)


@app.post(
    "/intake-items/{intake_id}/evidence-records",
    status_code=status.HTTP_201_CREATED,
)
async def create_evidence_record(
    intake_id: UUID,
    record: EvidenceRecordCreate,
    principal: Principal = Depends(require_permission("research:evidence:create")),
) -> dict:
    evidence_id = uuid4()
    event_id = uuid4()
    occurred_at = datetime.now(timezone.utc)
    try:
        async with app.state.pool.acquire() as connection:
            async with connection.transaction():
                intake = await connection.fetchrow(
                    """
                    SELECT correlation_id
                    FROM intake_items
                    WHERE intake_id = $1
                      AND tenant_id = $2
                    """,
                    intake_id,
                    principal.tenant_id,
                )
                if intake is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="intake item not found",
                    )
                correlation_id = intake["correlation_id"]
                await connection.execute(
                    """
                    INSERT INTO evidence_records (
                      evidence_id, tenant_id, intake_id, sha256, storage_reference,
                      media_type, filename, description, metadata, correlation_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
                    """,
                    evidence_id,
                    principal.tenant_id,
                    intake_id,
                    record.sha256,
                    record.storage_reference,
                    record.media_type,
                    record.filename,
                    record.description,
                    json.dumps(record.metadata),
                    correlation_id,
                )
                payload = {
                    "evidence_id": str(evidence_id),
                    "intake_id": str(intake_id),
                    "sha256": record.sha256,
                    "storage_reference": record.storage_reference,
                    "media_type": record.media_type,
                    "filename": record.filename,
                    "description": record.description,
                    "metadata": record.metadata,
                }
                await connection.execute(
                    """
                    INSERT INTO events (
                      event_id, event_type, occurred_at, producer,
                      correlation_id, schema_version, payload
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    """,
                    event_id,
                    "research.evidence.recorded",
                    occurred_at,
                    SERVICE_NAME,
                    correlation_id,
                    "1.0",
                    json.dumps(payload),
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database write failed",
        ) from exc

    return {
        "evidence_id": str(evidence_id),
        "event_id": str(event_id),
        "event_type": "research.evidence.recorded",
        "correlation_id": str(correlation_id),
        "occurred_at": serialize_datetime(occurred_at),
        "record": {
            "intake_id": str(intake_id),
            **payload,
        },
    }


@app.get("/intake-items/{intake_id}/evidence-records")
async def list_intake_evidence_records(
    intake_id: UUID,
    principal: Principal = Depends(require_permission("research:evidence:read")),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        async with app.state.pool.acquire() as connection:
            intake = await connection.fetchrow(
                """
                SELECT intake_id
                FROM intake_items
                WHERE intake_id = $1
                  AND tenant_id = $2
                """,
                intake_id,
                principal.tenant_id,
            )
            if intake is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="intake item not found",
                )
            rows = await connection.fetch(
                """
                SELECT
                  evidence_id, intake_id, sha256, storage_reference,
                  media_type, filename, description, metadata,
                  correlation_id, created_at
                FROM evidence_records
                WHERE intake_id = $1
                  AND tenant_id = $2
                ORDER BY created_at DESC, evidence_id DESC
                LIMIT $3 OFFSET $4
                """,
                intake_id,
                principal.tenant_id,
                limit,
                offset,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database query failed",
        ) from exc

    return {
        "intake_id": str(intake_id),
        "limit": limit,
        "offset": offset,
        "items": [serialize_evidence_record(row) for row in rows],
    }



@app.get("/intake-items/{intake_id}/events")
async def list_intake_events(
    intake_id: UUID,
    principal: Principal = Depends(require_permission("research:intake:read")),
) -> dict:
    async with app.state.pool.acquire() as connection:
        intake = await connection.fetchrow(
            """
            SELECT intake_id, correlation_id
            FROM intake_items
            WHERE intake_id = $1
              AND tenant_id = $2
            """,
            intake_id,
            principal.tenant_id,
        )

        if intake is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="intake item not found",
            )

        rows = await connection.fetch(
            """
            SELECT
              event_id,
              event_type,
              occurred_at,
              producer,
              correlation_id,
              schema_version,
              payload
            FROM events
            WHERE correlation_id = $1
            ORDER BY occurred_at ASC, event_id ASC
            """,
            intake["correlation_id"],
        )

    return {
        "intake_id": str(intake["intake_id"]),
        "correlation_id": str(intake["correlation_id"]),
        "events": [
            {
                "event_id": str(row["event_id"]),
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"].isoformat(),
                "producer": row["producer"],
                "correlation_id": str(row["correlation_id"]),
                "schema_version": row["schema_version"],
                "payload": row["payload"],
            }
            for row in rows
        ],
    }


@app.post("/approval-gates", status_code=status.HTTP_201_CREATED)
async def create_approval_gate(
    gate: ApprovalGateCreate,
    principal: Principal = Depends(require_permission("approval-gates:create")),
) -> dict:
    gate_id = uuid4()
    event_id = uuid4()
    correlation_id = gate.correlation_id or uuid4()
    occurred_at = datetime.now(timezone.utc)
    payload = {
        "gate_id": str(gate_id),
        "requester": gate.requester,
        "action_type": gate.action_type,
        "risk_level": gate.risk_level,
        "action_payload": gate.action_payload,
        "summary": gate.summary,
        "status": "pending",
    }

    try:
        async with app.state.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    """
                    INSERT INTO approval_gates (
                      gate_id, requester, action_type, risk_level,
                      action_payload, summary, status, correlation_id, tenant_id
                    )
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                    """,
                    gate_id,
                    gate.requester,
                    gate.action_type,
                    gate.risk_level,
                    json.dumps(gate.action_payload),
                    gate.summary,
                    "pending",
                    correlation_id,
                    principal.tenant_id,
                )
                await connection.execute(
                    """
                    INSERT INTO events (
                      event_id, event_type, occurred_at, producer,
                      correlation_id, schema_version, payload
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
                    """,
                    event_id,
                    "governance.approval_gate.created",
                    occurred_at,
                    SERVICE_NAME,
                    correlation_id,
                    "1.0",
                    json.dumps(payload),
                )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database write failed",
        ) from exc

    return {
        **payload,
        "correlation_id": str(correlation_id),
        "created_at": serialize_datetime(occurred_at),
        "event_id": str(event_id),
        "event_type": "governance.approval_gate.created",
    }


@app.get("/approval-gates/{gate_id}")
async def get_approval_gate(
    gate_id: UUID,
    principal: Principal = Depends(require_permission("approval-gates:read")),
) -> dict:
    try:
        async with app.state.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                  gate_id, requester, action_type, risk_level, action_payload,
                  summary, status, correlation_id, created_at, decided_at,
                  decided_by, decision_reason, decision_event_id
                FROM approval_gates
                WHERE gate_id = $1 AND tenant_id = $2
                """,
                gate_id,
                principal.tenant_id,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database query failed",
        ) from exc

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="approval gate not found",
        )

    return serialize_approval_gate(row)


@app.get("/approval-gates")
async def list_approval_gates(
    gate_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(require_permission("approval-gates:read")),
) -> dict:
    if gate_status is not None and gate_status not in APPROVAL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="status must be one of: pending, approved, rejected",
        )

    try:
        async with app.state.pool.acquire() as connection:
            if gate_status is None:
                rows = await connection.fetch(
                    """
                    SELECT
                      gate_id, requester, action_type, risk_level, action_payload,
                      summary, status, correlation_id, created_at, decided_at,
                      decided_by, decision_reason, decision_event_id
                    FROM approval_gates
                    WHERE tenant_id = $1
                    ORDER BY created_at DESC, gate_id DESC
                    LIMIT $2 OFFSET $3
                    """,
                    principal.tenant_id,
                    limit,
                    offset,
                )
            else:
                rows = await connection.fetch(
                    """
                    SELECT
                      gate_id, requester, action_type, risk_level, action_payload,
                      summary, status, correlation_id, created_at, decided_at,
                      decided_by, decision_reason, decision_event_id
                    FROM approval_gates
                    WHERE tenant_id = $1 AND status = $2
                    ORDER BY created_at DESC, gate_id DESC
                    LIMIT $3 OFFSET $4
                    """,
                    principal.tenant_id,
                    gate_status,
                    limit,
                    offset,
                )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database query failed",
        ) from exc

    return {
        "status": gate_status,
        "limit": limit,
        "offset": offset,
        "items": [serialize_approval_gate(row) for row in rows],
    }


async def decide_approval_gate(
    gate_id: UUID,
    decision: ApprovalDecisionCreate,
    next_status: str,
    principal: Principal,
) -> dict:
    event_id = uuid4()
    occurred_at = datetime.now(timezone.utc)
    event_type = f"governance.approval_gate.{next_status}"
    job_row = None
    job_event_type = None

    try:
        async with app.state.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE approval_gates
                    SET
                      status = $3,
                      decided_at = $4,
                      decided_by = $5,
                      decision_reason = $6,
                      decision_event_id = $7
                    WHERE gate_id = $1
                      AND tenant_id = $2
                      AND status = 'pending'
                    RETURNING
                      gate_id, requester, action_type, risk_level, action_payload,
                      summary, status, correlation_id, created_at, decided_at,
                      decided_by, decision_reason, decision_event_id
                    """,
                    gate_id,
                    principal.tenant_id,
                    next_status,
                    occurred_at,
                    decision.decided_by,
                    decision.decision_reason,
                    event_id,
                )
                if row is None:
                    existing = await connection.fetchrow(
                        """
                        SELECT status
                        FROM approval_gates
                        WHERE gate_id = $1 AND tenant_id = $2
                        """,
                        gate_id,
                        principal.tenant_id,
                    )
                    if existing is None:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="approval gate not found",
                        )
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="approval gate is no longer pending",
                    )

                await write_event(
                    connection,
                    event_type=event_type,
                    correlation_id=row["correlation_id"],
                    occurred_at=occurred_at,
                    payload={
                        "gate_id": str(gate_id),
                        "status": next_status,
                        "decided_by": decision.decided_by,
                        "decision_reason": decision.decision_reason,
                    },
                )

                next_job_status = (
                    "queued" if next_status == "approved" else "rejected"
                )
                completed_at = occurred_at if next_status == "rejected" else None

                job_row = await connection.fetchrow(
                    """
                    UPDATE jobs
                    SET status = $3, completed_at = $4
                    WHERE approval_gate_id = $1
                      AND tenant_id = $2
                      AND status = 'pending_approval'
                    RETURNING
                      job_id, correlation_id, status, completed_at
                    """,
                    gate_id,
                    principal.tenant_id,
                    next_job_status,
                    completed_at,
                )

                if job_row is not None:
                    job_event_type = (
                        "jobs.queued"
                        if next_status == "approved"
                        else "jobs.rejected"
                    )
                    await write_event(
                        connection,
                        event_type=job_event_type,
                        correlation_id=job_row["correlation_id"],
                        occurred_at=occurred_at,
                        payload={
                            "job_id": str(job_row["job_id"]),
                            "status": job_row["status"],
                            "approval_gate_id": str(gate_id),
                        },
                    )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database write failed",
        ) from exc

    response = {
        **serialize_approval_gate(row),
        "event_id": str(event_id),
        "event_type": event_type,
    }
    if job_row is not None:
        response["job_id"] = str(job_row["job_id"])
        response["job_status"] = job_row["status"]
        response["job_event_type"] = job_event_type
    return response


def serialize_job(row) -> dict:
    return {
        "job_id": str(row["job_id"]),
        "job_type": row["job_type"],
        "request_payload": decode_json_object(row["request_payload"]),
        "status": row["status"],
        "approval_required": row["approval_required"],
        "approval_gate_id": (
            str(row["approval_gate_id"])
            if row["approval_gate_id"] is not None
            else None
        ),
        "correlation_id": str(row["correlation_id"]),
        "result_payload": (
            decode_json_object(row["result_payload"])
            if row["result_payload"] is not None
            else None
        ),
        "error_code": row["error_code"],
        "created_at": serialize_datetime(row["created_at"]),
        "started_at": serialize_datetime(row["started_at"]),
        "completed_at": serialize_datetime(row["completed_at"]),
    }


async def write_event(
    connection,
    *,
    event_type: str,
    correlation_id: UUID,
    payload: dict,
    occurred_at: datetime | None = None,
) -> UUID:
    event_id = uuid4()
    await connection.execute(
        """
        INSERT INTO events (
          event_id, event_type, occurred_at, producer,
          correlation_id, schema_version, payload
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        """,
        event_id,
        event_type,
        occurred_at or datetime.now(timezone.utc),
        SERVICE_NAME,
        correlation_id,
        "1.0",
        json.dumps(payload),
    )
    return event_id


@app.post("/jobs", status_code=status.HTTP_201_CREATED)
async def create_job(
    job: JobCreate,
    principal: Principal = Depends(require_permission("jobs:create")),
) -> dict:
    job_id = uuid4()
    correlation_id = job.correlation_id or uuid4()
    created_at = datetime.now(timezone.utc)
    job_status = "pending_approval" if job.approval_required else "queued"
    approval_gate_id = uuid4() if job.approval_required else None

    try:
        async with app.state.pool.acquire() as connection:
            async with connection.transaction():
                if approval_gate_id is not None:
                    approval_summary = (
                        f"Approve execution of job {job_id} "
                        f"({job.job_type})."
                    )
                    approval_payload = {
                        "job_id": str(job_id),
                        "job_type": job.job_type,
                        "request_payload": job.request_payload,
                    }

                    await connection.execute(
                        """
                        INSERT INTO approval_gates (
                          gate_id, requester, action_type, risk_level,
                          action_payload, summary, status, correlation_id, tenant_id
                        )
                        VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                        """,
                        approval_gate_id,
                        SERVICE_NAME,
                        "jobs.execute",
                        "medium",
                        json.dumps(approval_payload),
                        approval_summary,
                        "pending",
                        correlation_id,
                        principal.tenant_id,
                    )

                    await write_event(
                        connection,
                        event_type="governance.approval_gate.created",
                        correlation_id=correlation_id,
                        occurred_at=created_at,
                        payload={
                            "gate_id": str(approval_gate_id),
                            "requester": SERVICE_NAME,
                            "action_type": "jobs.execute",
                            "risk_level": "medium",
                            "status": "pending",
                            "job_id": str(job_id),
                        },
                    )

                await connection.execute(
                    """
                    INSERT INTO jobs (
                      job_id, job_type, request_payload, status,
                      approval_required, approval_gate_id, correlation_id, tenant_id
                    )
                    VALUES ($1, $2, $3::jsonb, $4, $5, $6, $7, $8)
                    """,
                    job_id,
                    job.job_type,
                    json.dumps(job.request_payload),
                    job_status,
                    job.approval_required,
                    approval_gate_id,
                    correlation_id,
                    principal.tenant_id,
                )

                await write_event(
                    connection,
                    event_type="jobs.created",
                    correlation_id=correlation_id,
                    occurred_at=created_at,
                    payload={
                        "job_id": str(job_id),
                        "job_type": job.job_type,
                        "status": job_status,
                        "approval_required": job.approval_required,
                        "approval_gate_id": (
                            str(approval_gate_id)
                            if approval_gate_id is not None
                            else None
                        ),
                    },
                )

                await write_event(
                    connection,
                    event_type=(
                        "jobs.approval_requested"
                        if job.approval_required
                        else "jobs.queued"
                    ),
                    correlation_id=correlation_id,
                    occurred_at=created_at,
                    payload={
                        "job_id": str(job_id),
                        "status": job_status,
                        "approval_gate_id": (
                            str(approval_gate_id)
                            if approval_gate_id is not None
                            else None
                        ),
                    },
                )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database write failed",
        ) from exc

    return {
        "job_id": str(job_id),
        "job_type": job.job_type,
        "request_payload": job.request_payload,
        "status": job_status,
        "approval_required": job.approval_required,
        "approval_gate_id": (
            str(approval_gate_id)
            if approval_gate_id is not None
            else None
        ),
        "correlation_id": str(correlation_id),
        "result_payload": None,
        "error_code": None,
        "created_at": serialize_datetime(created_at),
        "started_at": None,
        "completed_at": None,
    }


@app.get("/jobs/{job_id}")
async def get_job(
    job_id: UUID,
    principal: Principal = Depends(require_permission("jobs:read")),
) -> dict:
    try:
        async with app.state.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                  job_id, job_type, request_payload, status,
                  approval_required, approval_gate_id, correlation_id,
                  result_payload, error_code, created_at, started_at, completed_at
                FROM jobs
                WHERE job_id = $1 AND tenant_id = $2
                """,
                job_id,
                principal.tenant_id,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database query failed",
        ) from exc

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="job not found",
        )

    return serialize_job(row)


@app.post("/jobs/{job_id}/execute")
async def execute_job(
    job_id: UUID,
    principal: Principal = Depends(require_permission("jobs:execute")),
) -> dict:
    started_at = datetime.now(timezone.utc)

    try:
        async with app.state.pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    """
                    UPDATE jobs
                    SET status = 'running', started_at = $3
                    WHERE job_id = $1
                      AND tenant_id = $2
                      AND status = 'queued'
                    RETURNING
                      job_id, job_type, request_payload, status,
                      approval_required, approval_gate_id, correlation_id,
                      result_payload, error_code, created_at, started_at, completed_at
                    """,
                    job_id,
                    principal.tenant_id,
                    started_at,
                )

                if row is None:
                    existing = await connection.fetchrow(
                        """
                        SELECT status
                        FROM jobs
                        WHERE job_id = $1 AND tenant_id = $2
                        """,
                        job_id,
                        principal.tenant_id,
                    )
                    if existing is None:
                        raise HTTPException(
                            status_code=status.HTTP_404_NOT_FOUND,
                            detail="job not found",
                        )
                    if existing["status"] == "pending_approval":
                        raise HTTPException(
                            status_code=status.HTTP_409_CONFLICT,
                            detail="job is awaiting approval",
                        )
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="job is not queued",
                    )

                await write_event(
                    connection,
                    event_type="jobs.started",
                    correlation_id=row["correlation_id"],
                    occurred_at=started_at,
                    payload={
                        "job_id": str(row["job_id"]),
                        "status": "running",
                    },
                )

                request_payload = decode_json_object(row["request_payload"])
                content = request_payload["content"]
                completed_at = datetime.now(timezone.utc)
                result_payload = {
                    "char_count": len(content),
                    "word_count": len(content.split()),
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "analyzed_at": serialize_datetime(completed_at),
                }

                completed = await connection.fetchrow(
                    """
                    UPDATE jobs
                    SET
                      status = 'succeeded',
                      result_payload = $3::jsonb,
                      completed_at = $4
                    WHERE job_id = $1
                      AND tenant_id = $2
                      AND status = 'running'
                    RETURNING
                      job_id, job_type, request_payload, status,
                      approval_required, approval_gate_id, correlation_id,
                      result_payload, error_code, created_at, started_at, completed_at
                    """,
                    job_id,
                    principal.tenant_id,
                    json.dumps(result_payload),
                    completed_at,
                )

                if completed is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="job could not be completed",
                    )

                await write_event(
                    connection,
                    event_type="jobs.succeeded",
                    correlation_id=completed["correlation_id"],
                    occurred_at=completed_at,
                    payload={
                        "job_id": str(completed["job_id"]),
                        "status": "succeeded",
                        "result_sha256": result_payload["sha256"],
                    },
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database write failed",
        ) from exc

    return serialize_job(completed)


@app.post("/approval-gates/{gate_id}/approve")
async def approve_approval_gate(
    gate_id: UUID,
    decision: ApprovalDecisionCreate,
    principal: Principal = Depends(require_permission("approval-gates:decide")),
) -> dict:
    return await decide_approval_gate(gate_id, decision, "approved", principal)


@app.post("/approval-gates/{gate_id}/reject")
async def reject_approval_gate(
    gate_id: UUID,
    decision: ApprovalDecisionCreate,
    principal: Principal = Depends(require_permission("approval-gates:decide")),
) -> dict:
    return await decide_approval_gate(gate_id, decision, "rejected", principal)
