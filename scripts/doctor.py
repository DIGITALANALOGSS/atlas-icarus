#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

checks = [
    ("Docker CLI", ("docker", "--version")),
    ("Docker Compose", ("docker", "compose", "version")),
    ("Git", ("git", "--version")),
    ("Python", (sys.executable, "--version")),
]

failed = False

print("Atlas ICARUS environment check")
print(f"Repository: {ROOT}")

for label, command in checks:
    executable = command[0]
    if executable != sys.executable and shutil.which(executable) is None:
        print(f"FAIL  {label}: command not found")
        failed = True
        continue

    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    output = (result.stdout or result.stderr).strip().splitlines()
    if result.returncode == 0:
        print(f"PASS  {label}: {output[0] if output else 'available'}")
    else:
        print(f"FAIL  {label}: {' '.join(output) if output else f'exit {result.returncode}'}")
        failed = True

for relative_path in (".env", "compose.yaml", "services/atlas-control/Dockerfile"):
    path = ROOT / relative_path
    if path.is_file():
        print(f"PASS  Required file: {relative_path}")
    else:
        print(f"FAIL  Required file missing: {relative_path}")
        failed = True

result = subprocess.run(
    ("git", "status", "--short"),
    cwd=ROOT,
    text=True,
    capture_output=True,
)
if result.returncode == 0:
    status = result.stdout.strip()
    print("PASS  Git working tree clean" if not status else f"WARN  Git working tree has changes:\n{status}")
else:
    print("FAIL  Could not read Git status")
    failed = True

if failed:
    print("\nRESULT: NOT READY")
    raise SystemExit(1)

print("\nRESULT: READY")
