# Atlas ICARUS Developer Workflow

Run these commands from the repository root.

## Daily workflow

```bash
make doctor
make up
make verify
```

`make verify` checks Git whitespace, runs the containerized test profile, starts Atlas Control, waits for database-backed readiness, and runs a live intake-to-evidence-to-audit smoke workflow.

## Commands

| Command | Purpose |
|---|---|
| `make doctor` | Check required local tools, expected project files, and Git state |
| `make up` | Build/start Atlas Control and wait for `/readyz` |
| `make down` | Stop services while preserving local PostgreSQL data |
| `make test` | Run the isolated containerized Atlas Control test suite |
| `make smoke` | Run the live synthetic intake/evidence/audit smoke workflow |
| `make verify` | Run whitespace check, tests, readiness, and the smoke workflow |
| `make status` | Show Compose service state and recent Git history |
| `make logs` | Follow Atlas Control logs |

## Safety

The smoke workflow creates synthetic metadata records in the local PostgreSQL development database. It does not store original files, access external systems, modify source code, create commits, or push to GitHub.

Use `make down` for normal shutdown. It preserves the local PostgreSQL volume. Do not use `docker compose down -v` unless you intentionally want to erase all local development data.
