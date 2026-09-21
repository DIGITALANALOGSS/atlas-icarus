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
from fastapi import FastAPI, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

SERVICE_NAME = "atlas-control"
SERVICE_VERSION = "0.1.0"
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("received_at must include a UTC offset or Z")
    return value.astimezone(timezone.utc)


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
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

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
async def create_intake_item(item: IntakeItemCreate) -> dict:
    intake_id = uuid4()
    event_id = uuid4()
    correlation_id = item.correlation_id or uuid4()
    occurred_at = datetime.now(timezone.utc)
    payload = {
        "intake_id": str(intake_id),
        "source": item.source,
        "received_at": item.received_at.isoformat().replace("+00:00", "Z"),
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
                      intake_id, source, received_at, custodian,
                      storage_reference, original_sha256, notes, correlation_id
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    """,
                    intake_id,
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
        "occurred_at": occurred_at.isoformat().replace("+00:00", "Z"),
        "item": payload,
    }


@app.get("/intake-items/{intake_id}/events")
async def list_intake_events(intake_id: UUID) -> dict:
    try:
        async with app.state.pool.acquire() as connection:
            intake = await connection.fetchrow(
                """
                SELECT intake_id, correlation_id
                FROM intake_items
                WHERE intake_id = $1
                """,
                intake_id,
            )
            if intake is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="intake item not found",
                )

            rows = await connection.fetch(
                """
                SELECT
                  event_id, event_type, occurred_at, producer,
                  correlation_id, schema_version, payload
                FROM events
                WHERE correlation_id = $1
                ORDER BY occurred_at ASC, event_id ASC
                """,
                intake["correlation_id"],
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database query failed",
        ) from exc

    events = [
        {
            "event_id": str(row["event_id"]),
            "event_type": row["event_type"],
            "occurred_at": row["occurred_at"].isoformat().replace("+00:00", "Z"),
            "producer": row["producer"],
            "correlation_id": str(row["correlation_id"]),
            "schema_version": row["schema_version"],
            "payload": row["payload"],
        }
        for row in rows
    ]

    return {
        "intake_id": str(intake["intake_id"]),
        "correlation_id": str(intake["correlation_id"]),
        "events": events,
        "count": len(events),
    }


@app.get("/intake-items/{intake_id}")
async def get_intake_item(intake_id: UUID) -> dict:
    try:
        async with app.state.pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT
                  intake_id, source, received_at, custodian,
                  storage_reference, original_sha256, notes,
                  correlation_id, created_at
                FROM intake_items
                WHERE intake_id = $1
                """,
                intake_id,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database query failed",
        ) from exc

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="intake item not found",
        )

    return {
        "intake_id": str(row["intake_id"]),
        "source": row["source"],
        "received_at": row["received_at"].isoformat().replace("+00:00", "Z"),
        "custodian": row["custodian"],
        "storage_reference": row["storage_reference"],
        "original_sha256": row["original_sha256"],
        "notes": row["notes"],
        "correlation_id": str(row["correlation_id"]),
        "created_at": row["created_at"].isoformat().replace("+00:00", "Z"),
    }


@app.get("/intake-items")
async def list_intake_items(
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    try:
        async with app.state.pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                  intake_id, source, received_at, custodian,
                  storage_reference, original_sha256, notes,
                  correlation_id, created_at
                FROM intake_items
                ORDER BY created_at DESC
                LIMIT $1
                """,
                limit,
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="database query failed",
        ) from exc

    items = [
        {
            "intake_id": str(row["intake_id"]),
            "source": row["source"],
            "received_at": row["received_at"].isoformat().replace("+00:00", "Z"),
            "custodian": row["custodian"],
            "storage_reference": row["storage_reference"],
            "original_sha256": row["original_sha256"],
            "notes": row["notes"],
            "correlation_id": str(row["correlation_id"]),
            "created_at": row["created_at"].isoformat().replace("+00:00", "Z"),
        }
        for row in rows
    ]

    return {"items": items, "count": len(items)}
