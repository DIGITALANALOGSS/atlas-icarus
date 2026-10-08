#!/usr/bin/env python3
"""Operate Atlas jobs through the existing HTTP API."""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from uuid import UUID


class CLIError(Exception):
    pass


def uuid_value(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("must be a UUID") from exc


def validate_base_url(value):
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise CLIError("invalid API base URL") from exc
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise CLIError("base URL must be an HTTP(S) origin without credentials")
    if parsed.scheme == "http" and parsed.hostname not in (
        "localhost", "127.0.0.1", "::1"
    ):
        raise CLIError("non-loopback APIs require HTTPS")
    return value.rstrip("/")


def read_content(filename):
    try:
        content = Path(filename).read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise CLIError("cannot read input as a UTF-8 text file") from exc
    if not content.strip():
        raise CLIError("input must contain nonblank text")
    if len(content) > 20000:
        raise CLIError("input must not exceed 20000 characters")
    return content


class APIClient:
    def __init__(self, base_url, token):
        self.base_url = validate_base_url(base_url)
        if not token or not token.strip():
            raise CLIError("set ATLAS_TOKEN to an authorized bearer token")
        if any(character.isspace() for character in token):
            raise CLIError("ATLAS_TOKEN must not contain whitespace")
        self.token = token

    def request(self, method, path, payload=None):
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=15) as response:
                raw = response.read()
        except HTTPError as exc:
            # Do not echo arbitrary server bodies, URLs, or credentials.
            hints = {
                401: "authentication required or token rejected",
                403: "permission denied",
                404: "resource missing or not visible to this tenant",
                409: "operation conflicts with current job or gate state",
                422: "request validation failed",
                503: "service or database operation unavailable",
            }
            hint = hints.get(exc.code, "API request failed")
            raise CLIError(f"HTTP {exc.code}: {hint}") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise CLIError("API connection failed; check service and base URL") from None
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise CLIError("API returned invalid JSON") from None
        if not isinstance(result, dict):
            raise CLIError("API returned an unexpected response shape")
        return result


def parser():
    result = argparse.ArgumentParser(
        description="Operate Atlas jobs; submit only a separate working copy."
    )
    result.add_argument(
        "--base-url",
        default=os.environ.get("ATLAS_BASE_URL", "http://127.0.0.1:8000"),
    )
    commands = result.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit", help="submit UTF-8 working-copy text")
    submit.add_argument("file")
    submit.add_argument(
        "--ungated", action="store_true",
        help="explicitly create a job without an approval gate",
    )
    submit.add_argument("--correlation-id", type=uuid_value)
    submit.add_argument("--evidence-id", type=uuid_value)
    for name in ("show", "execute", "result", "approve", "reject"):
        command = commands.add_parser(name)
        command.add_argument("job_id", type=uuid_value)
        if name in ("approve", "reject"):
            command.add_argument("--decided-by", required=True)
            command.add_argument("--reason")
    return result


def dispatch(args, client):
    if args.command == "submit":
        payload = {
            "job_type": "metadata.analyze",
            "request_payload": {"content": read_content(args.file)},
            "approval_required": not args.ungated,
        }
        if args.evidence_id:
            payload["evidence_id"] = args.evidence_id
        if args.correlation_id:
            payload["correlation_id"] = args.correlation_id
        return client.request("POST", "/jobs", payload)

    path = f"/jobs/{args.job_id}"
    if args.command == "show":
        return client.request("GET", path)
    if args.command == "execute":
        return client.request("POST", path + "/execute")
    if args.command == "result":
        return client.request("GET", path + "/workflow-result")

    decided_by = args.decided_by.strip()
    if not decided_by or len(decided_by) > 1000:
        raise CLIError("decided-by must contain 1 to 1000 characters")
    reason = args.reason.strip() if args.reason is not None else None
    if reason is not None and len(reason) > 2000:
        raise CLIError("reason must not exceed 2000 characters")
    job = client.request("GET", path)
    gate = job.get("approval_gate_id")
    if not gate:
        raise CLIError("job has no linked approval gate")
    try:
        gate = str(UUID(str(gate)))
    except ValueError:
        raise CLIError("API returned an invalid approval gate identifier") from None
    payload = {"decided_by": decided_by}
    if reason:
        payload["decision_reason"] = reason
    return client.request(
        "POST", f"/approval-gates/{gate}/{args.command}", payload
    )


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        client = APIClient(args.base_url, os.environ.get("ATLAS_TOKEN", ""))
        result = dispatch(args, client)
    except CLIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
