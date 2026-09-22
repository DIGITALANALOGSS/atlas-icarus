#!/usr/bin/env bash
set -euo pipefail

printf '== Whitespace check ==\n'
git diff --check

printf '\n== Containerized tests ==\n'
docker compose -f compose.yaml --profile test run --rm --build atlas-control-tests

printf '\n== Start and wait for service ==\n'
docker compose -f compose.yaml up -d --build atlas-control
python3 scripts/smoke_evidence_workflow.py --ready-only

printf '\n== Live smoke workflow ==\n'
python3 scripts/smoke_evidence_workflow.py

printf '\nRESULT: VERIFY PASSED\n'
