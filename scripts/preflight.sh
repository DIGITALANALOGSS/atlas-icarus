#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  printf 'Error: run this command from inside a Git checkout.\n' >&2
  exit 1
}

cd "$repo_root"

printf '\n=== GIT STATUS ===\n'
git status --short --branch

printf '\n=== WHITESPACE CHECK ===\n'
git diff --check
git diff --cached --check

printf '\n=== MIGRATION FILE ORDER ===\n'
find services/atlas-control/migrations -maxdepth 1 -type f -name '*.sql' \
  -printf '%f\n' | sort

printf '\n=== POSTGRES SERVICE ===\n'
docker compose ps postgres

printf '\n=== DATABASE MIGRATION LEDGER ===\n'
docker compose exec -T postgres \
  psql -U "${POSTGRES_USER:-atlas}" -d "${POSTGRES_DB:-atlas}" \
  -Atc 'SELECT version FROM schema_migrations ORDER BY version;'

printf '\n=== SERVICE HEALTH ===\n'
curl -fsS --max-time 5 http://127.0.0.1:8000/healthz
printf '\n'

printf '\n=== SERVICE READINESS ===\n'
curl -fsS --max-time 5 http://127.0.0.1:8000/readyz
printf '\n'

printf '\nPreflight passed.\n'
