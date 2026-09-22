#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

BASE_URL = "http://127.0.0.1:8000"


def fail(message: str) -> None:
    print(f"FAIL  {message}", file=sys.stderr)
    raise SystemExit(1)


def request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request_object = Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request_object, timeout=10) as response:
            raw = response.read().decode()
            return response.status, json.loads(raw)
    except HTTPError as error:
        raw = error.read().decode()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        return error.code, body


def wait_for_ready(timeout_seconds: int = 60) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_error = "service did not respond"

    while time.monotonic() < deadline:
        try:
            status_code, body = request("GET", "/readyz")
            if status_code == 200 and body.get("database") == "reachable":
                return body
            last_error = f"HTTP {status_code}: {body}"
        except (
            ConnectionError,
            ConnectionResetError,
            socket.timeout,
            TimeoutError,
            URLError,
            OSError,
        ) as error:
            last_error = f"{type(error).__name__}: {error}"
        time.sleep(2)

    fail(f"/readyz did not become healthy within {timeout_seconds}s: {last_error}")


def expect(status_code: int, expected: int, operation: str, body: dict) -> None:
    if status_code != expected:
        fail(f"{operation}: expected HTTP {expected}, got {status_code}: {json.dumps(body)}")
    print(f"PASS  {operation}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify the live Atlas Control intake/evidence/audit workflow."
    )
    parser.add_argument(
        "--ready-only",
        action="store_true",
        help="Wait only for /readyz to report a reachable database.",
    )
    args = parser.parse_args()

    ready = wait_for_ready()
    print(
        "PASS  readiness: "
        f"{ready.get('service')} {ready.get('version')} database={ready.get('database')}"
    )
    if args.ready_only:
        print("RESULT: READY")
        return

    run_id = uuid4().hex
    received_at = (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    intake_sha256 = hashlib.sha256(f"atlas-intake-{run_id}".encode()).hexdigest()
    evidence_sha256 = hashlib.sha256(f"atlas-evidence-{run_id}".encode()).hexdigest()

    intake_payload = {
        "source": "atlas-automation-smoke",
        "received_at": received_at,
        "custodian": "Atlas automation",
        "storage_reference": f"controlled://atlas-local/intake/{run_id}",
        "original_sha256": intake_sha256,
        "notes": f"Synthetic automation smoke record {run_id}.",
    }
    status_code, intake = request("POST", "/intake-items", intake_payload)
    expect(status_code, 201, "create intake", intake)

    intake_id = intake.get("intake_id")
    correlation_id = intake.get("correlation_id")
    if not intake_id or not correlation_id:
        fail(f"create intake: missing intake_id or correlation_id: {json.dumps(intake)}")

    evidence_payload = {
        "sha256": evidence_sha256,
        "storage_reference": f"controlled://atlas-local/evidence/{run_id}.txt",
        "media_type": "text/plain",
        "filename": f"smoke-{run_id}.txt",
        "description": "Synthetic evidence metadata created by the Atlas smoke workflow.",
        "metadata": {"run_id": run_id, "purpose": "automation-smoke"},
    }
    status_code, evidence = request(
        "POST",
        f"/intake-items/{intake_id}/evidence-records",
        evidence_payload,
    )
    expect(status_code, 201, "create evidence record", evidence)

    evidence_id = evidence.get("evidence_id")
    if not evidence_id:
        fail(f"create evidence record: missing evidence_id: {json.dumps(evidence)}")
    if evidence.get("correlation_id") != correlation_id:
        fail("create evidence record: correlation ID did not match the intake")

    status_code, fetched_evidence = request("GET", f"/evidence-records/{evidence_id}")
    expect(status_code, 200, "get evidence record", fetched_evidence)
    if fetched_evidence.get("intake_id") != intake_id:
        fail("get evidence record: intake ID did not match")
    if fetched_evidence.get("sha256") != evidence_sha256:
        fail("get evidence record: SHA-256 did not match")

    status_code, evidence_list = request(
        "GET",
        f"/intake-items/{intake_id}/evidence-records?limit=10&offset=0",
    )
    expect(status_code, 200, "list intake evidence records", evidence_list)
    listed_ids = {item.get("evidence_id") for item in evidence_list.get("items", [])}
    if evidence_id not in listed_ids:
        fail("list intake evidence records: created evidence ID was absent")

    status_code, events = request("GET", f"/intake-items/{intake_id}/events")
    expect(status_code, 200, "list intake events", events)
    if events.get("correlation_id") != correlation_id:
        fail("list intake events: correlation ID did not match")
    event_types = {event.get("event_type") for event in events.get("events", [])}
    expected_event_types = {"research.intake.created", "research.evidence.recorded"}
    if not expected_event_types.issubset(event_types):
        fail(
            "list intake events: missing expected event types: "
            f"{sorted(expected_event_types - event_types)}"
        )

    print(f"PASS  correlation integrity: {correlation_id}")
    print(f"PASS  synthetic workflow ID: {run_id}")
    print("RESULT: SMOKE TEST PASSED")


if __name__ == "__main__":
    main()
