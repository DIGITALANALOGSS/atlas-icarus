#!/usr/bin/env python3
"""Create verified local copies and a manifest; never register them remotely."""

import argparse
import hashlib
import json
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


REPOSITORY = Path(__file__).resolve().parent.parent
CHUNK_SIZE = 1024 * 1024


class PreservationError(Exception):
    pass


def utc_timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def received_timestamp(value):
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError("received-at must be an ISO 8601 timestamp")
    if timestamp.tzinfo is None:
        raise argparse.ArgumentTypeError("received-at must include a timezone")
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def digest_file(path):
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def signature(info):
    return (
        info.st_dev, info.st_ino, info.st_size,
        info.st_mtime_ns, info.st_ctime_ns,
    )


def write_json(path, value):
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def preserve(source, destination, *, custodian, source_label, received_at):
    custodian = custodian.strip()
    source_label = source_label.strip()
    if not custodian or len(custodian) > 1000:
        raise PreservationError("custodian must contain 1 to 1000 characters")
    if not source_label or len(source_label) > 1000:
        raise PreservationError("source label must contain 1 to 1000 characters")
    try:
        received_at = received_timestamp(received_at)
    except argparse.ArgumentTypeError as exc:
        raise PreservationError(str(exc)) from exc

    source = Path(source).absolute()
    destination = Path(destination)
    try:
        destination = destination.resolve(strict=True)
    except OSError:
        raise PreservationError("destination must be an existing directory") from None
    if not destination.is_dir():
        raise PreservationError("destination must be an existing directory")
    if destination == REPOSITORY or REPOSITORY in destination.parents:
        raise PreservationError("destination must be outside the Git repository")
    if source.is_symlink():
        raise PreservationError("source must not be a symbolic link")
    if not hasattr(os, "O_NOFOLLOW"):
        raise PreservationError("this platform lacks required no-follow file support")

    folder = None
    try:
        descriptor = os.open(
            source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        )
        with os.fdopen(descriptor, "rb") as original:
            before = os.fstat(original.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise PreservationError("source must be a regular file")

            preservation_id = str(uuid4())
            folder = destination / preservation_id
            folder.mkdir(mode=0o700, exist_ok=False)
            preserved_dir = folder / "preserved"
            working_dir = folder / "working"
            preserved_dir.mkdir(mode=0o700)
            working_dir.mkdir(mode=0o700)
            preserved = preserved_dir / "content.bin"
            working = working_dir / "content.bin"

            digest = hashlib.sha256()
            size = 0
            output = os.open(
                preserved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(output, "wb") as copied:
                while chunk := original.read(CHUNK_SIZE):
                    copied.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                copied.flush()
                os.fsync(copied.fileno())

            if signature(before) != signature(os.fstat(original.fileno())):
                raise PreservationError("source changed during copying")

            original.seek(0)
            reread = hashlib.sha256()
            while chunk := original.read(CHUNK_SIZE):
                reread.update(chunk)
            if (
                reread.hexdigest() != digest.hexdigest()
                or signature(before) != signature(os.fstat(original.fileno()))
                or signature(before) != signature(os.stat(source, follow_symlinks=False))
            ):
                raise PreservationError("source changed during verification")

        expected = (digest.hexdigest(), size)
        if digest_file(preserved) != expected:
            raise PreservationError("preserved-copy verification failed")

        output = os.open(
            working, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with preserved.open("rb") as input_stream:
            with os.fdopen(output, "wb") as copied:
                while chunk := input_stream.read(CHUNK_SIZE):
                    copied.write(chunk)
                copied.flush()
                os.fsync(copied.fileno())
        if digest_file(working) != expected or digest_file(preserved) != expected:
            raise PreservationError("copy verification failed")

        manifest = {
            "schema_version": "1",
            "preservation_id": preservation_id,
            "status": "verified",
            "created_at": utc_timestamp(),
            "received_at": received_at,
            "custodian": custodian,
            "source": source_label,
            "source_path": str(source),
            "original_filename": source.name,
            "size_bytes": size,
            "sha256": digest.hexdigest(),
            "preserved_copy": str(preserved),
            "working_copy": str(working),
            "verification_scope": "byte identity at collection time",
        }
        write_json(folder / "manifest.json", manifest)
        return {**manifest, "manifest_path": str(folder / "manifest.json")}

    except Exception as exc:
        if folder is not None:
            try:
                write_json(folder / "FAILED.json", {
                    "status": "failed",
                    "created_at": utc_timestamp(),
                    "instruction": "Partial copies retained; do not treat as verified.",
                })
            except Exception:
                pass
            raise PreservationError(
                f"preservation failed; inspect retained directory: {folder}"
            ) from exc
        if isinstance(exc, PreservationError):
            raise
        raise PreservationError("cannot open source or create preservation directory") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preserve a regular file locally, outside the repository."
    )
    parser.add_argument("source_file")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--custodian", required=True)
    parser.add_argument("--source-label", required=True)
    parser.add_argument("--received-at", required=True, type=received_timestamp)
    args = parser.parse_args(argv)
    try:
        result = preserve(
            args.source_file, args.destination,
            custodian=args.custodian,
            source_label=args.source_label,
            received_at=args.received_at,
        )
    except PreservationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
