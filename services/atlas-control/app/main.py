import os
from datetime import datetime, timezone

import asyncpg
from fastapi import FastAPI
from fastapi.responses import JSONResponse

SERVICE_NAME = "atlas-control"
SERVICE_VERSION = "0.1.0"

app = FastAPI(title=SERVICE_NAME, version=SERVICE_VERSION)


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "timestamp_utc": timestamp_utc(),
                "reason": "DATABASE_URL is not configured",
            },
        )

    try:
        connection = await asyncpg.connect(database_url, timeout=3)
        await connection.execute("SELECT 1")
        await connection.close()
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "timestamp_utc": timestamp_utc(),
                "reason": "database connection failed",
            },
        )

    return {
        "status": "ok",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "timestamp_utc": timestamp_utc(),
        "database": "reachable",
    }
