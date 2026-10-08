#!/usr/bin/env python3
"""Explicitly register verified local preservation copies with Atlas."""

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID, uuid4

from atlas_cli import APIClient, CLIError
from atlas_preserve import (
    REPOSITORY, digest_file, received_timestamp, utc_timestamp, write_json,
)


class RegistrationError(Exception):
    pass


def verified_manifest(filename):
    path = Path(filename).resolve(strict=True)
    if path == REPOSITORY or REPOSITORY in path.parents:
        raise RegistrationError("manifest must be outside the repository")
    if path.name != "manifest.json":
        raise RegistrationError("expected a preservation manifest.json")
    folder = path.parent
    if (folder / "FAILED.json").exists():
        raise RegistrationError("preservation directory is marked failed")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RegistrationError("manifest must be a JSON object")
    if data.get("schema_version") != "1" or data.get("status") != "verified":
        raise RegistrationError("unsupported or unverified preservation manifest")
    preservation_id = str(UUID(data["preservation_id"]))
    if folder.name != preservation_id:
        raise RegistrationError("manifest directory does not match preservation ID")
    sha256 = data["sha256"]
    if (
        not isinstance(sha256, str) or len(sha256) != 64
        or any(character not in "0123456789abcdef" for character in sha256)
    ):
        raise RegistrationError("manifest contains an invalid digest")
    size = data["size_bytes"]
    if type(size) is not int or size < 0:
        raise RegistrationError("manifest contains an invalid size")
    for key, limit in (
        ("source", 1000), ("custodian", 1000), ("original_filename", 1000)
    ):
        value = data[key]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise RegistrationError(f"manifest contains invalid {key}")
    received_timestamp(data["received_at"])
    paths = {}
    for key, subdirectory in (
        ("preserved_copy", "preserved"), ("working_copy", "working")
    ):
        expected = folder / subdirectory / "content.bin"
        declared = Path(data[key])
        if (
            not declared.is_absolute() or declared != expected
            or expected.is_symlink() or expected.parent.is_symlink()
            or not expected.is_file()
            or expected.resolve(strict=True) != expected
            or len(str(expected)) > 2000
        ):
            raise RegistrationError("copy path does not match preservation layout")
        if digest_file(expected) != (sha256, size):
            raise RegistrationError(f"{key} no longer matches the manifest")
        paths[key] = expected
    if os.path.samefile(paths["preserved_copy"], paths["working_copy"]):
        raise RegistrationError("preserved and working copies must be separate")
    return path, data


def save_receipt(path, receipt):
    temporary = path.parent / f".registration-{uuid4()}.tmp"
    try:
        write_json(temporary, receipt)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def response_uuid(response, key):
    if not isinstance(response, dict):
        raise RegistrationError("API returned an invalid registration response")
    try:
        return str(UUID(response[key]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise RegistrationError("API response did not contain a valid identifier") from None


def register(filename, client):
    try:
        manifest_path, manifest = verified_manifest(filename)
    except RegistrationError:
        raise
    except Exception:
        raise RegistrationError("cannot validate preservation manifest and copies") from None

    receipt_path = manifest_path.parent / "registration.json"
    receipt = {
        "schema_version": "1",
        "preservation_id": manifest["preservation_id"],
        "api_base_url": client.base_url,
        "correlation_id": str(uuid4()),
        "status": "prepared",
        "created_at": utc_timestamp(),
        "intake_id": None,
        "evidence_id": None,
    }
    try:
        write_json(receipt_path, receipt)
    except FileExistsError:
        raise RegistrationError(
            "registration receipt already exists; inspect it instead of retrying"
        ) from None
    except OSError:
        raise RegistrationError("cannot create registration receipt; no request sent") from None

    stage = "prepared"
    try:
        stage = "intake_request_pending"
        receipt["status"] = stage
        save_receipt(receipt_path, receipt)
        intake = client.request("POST", "/intake-items", {
            "source": manifest["source"],
            "received_at": manifest["received_at"],
            "custodian": manifest["custodian"],
            "storage_reference": manifest["preserved_copy"],
            "original_sha256": manifest["sha256"],
            "correlation_id": receipt["correlation_id"],
            "notes": "Registered from a locally verified preservation manifest.",
        })
        receipt["intake_id"] = response_uuid(intake, "intake_id")
        receipt["status"] = "intake_registered"
        save_receipt(receipt_path, receipt)

        stage = "evidence_request_pending"
        receipt["status"] = stage
        save_receipt(receipt_path, receipt)
        evidence = client.request(
            "POST",
            f"/intake-items/{receipt['intake_id']}/evidence-records",
            {
                "sha256": manifest["sha256"],
                "storage_reference": manifest["working_copy"],
                "filename": manifest["original_filename"],
                "description": "Verified working copy from local preservation.",
                "metadata": {
                    "preservation_id": manifest["preservation_id"],
                    "size_bytes": manifest["size_bytes"],
                },
            },
        )
        receipt["evidence_id"] = response_uuid(evidence, "evidence_id")
        receipt["status"] = "registered"
        receipt["completed_at"] = utc_timestamp()
        save_receipt(receipt_path, receipt)
        return {**receipt, "receipt_path": str(receipt_path)}
    except Exception:
        receipt["status"] = "needs_review"
        receipt["last_stage"] = stage
        receipt["updated_at"] = utc_timestamp()
        receipt["instruction"] = (
            "Inspect receipt and server records; requests may have committed. "
            "Do not remove this receipt or retry blindly."
        )
        try:
            save_receipt(receipt_path, receipt)
        except Exception:
            pass
        raise RegistrationError(
            f"registration incomplete or uncertain; inspect {receipt_path}"
        ) from None


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify copies and explicitly register intake/evidence metadata."
    )
    parser.add_argument("manifest")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("ATLAS_BASE_URL", "http://127.0.0.1:8000"),
    )
    args = parser.parse_args(argv)
    try:
        client = APIClient(args.base_url, os.environ.get("ATLAS_TOKEN", ""))
        result = register(args.manifest, client)
    except (CLIError, RegistrationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
