#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s <migration-filename>\n' "$0" >&2
  printf 'Example: %s 013_conversations.sql\n' "$0" >&2
  exit 64
}

if [ "$#" -ne 1 ]; then
  usage
fi

migration_name="$1"

case "$migration_name" in
  */*|*'..'*|'')
    printf 'Error: pass a migration filename only, not a path.\n' >&2
    exit 64
    ;;
esac

case "$migration_name" in
  *.sql) ;;
  *)
    printf 'Error: migration filename must end in .sql.\n' >&2
    exit 64
    ;;
esac

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)" || {
  printf 'Error: run this command from inside a Git checkout.\n' >&2
  exit 1
}

cd "$repo_root"

migration_path="services/atlas-control/migrations/$migration_name"

if [ ! -f "$migration_path" ]; then
  printf 'Error: migration not found: %s\n' "$migration_path" >&2
  exit 66
fi

if ! docker compose ps --status running postgres >/dev/null 2>&1; then
  printf 'Error: postgres service is not running.\n' >&2
  exit 69
fi

printf 'Validating %s inside a transaction that will be rolled back...\n' "$migration_path"

{
  printf 'BEGIN;\n'
  cat "$migration_path"
  printf '\nROLLBACK;\n'
} | docker compose exec -T postgres \
  psql -U "${POSTGRES_USER:-atlas}" -d "${POSTGRES_DB:-atlas}" \
  -v ON_ERROR_STOP=1

printf 'Validation passed; transaction was rolled back.\n'
