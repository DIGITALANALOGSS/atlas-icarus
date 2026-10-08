# Atlas ICARUS

Atlas ICARUS is a local-first platform foundation built around the Atlas
Control FastAPI service, PostgreSQL, and Docker Compose.

## Implemented capabilities

- Tenant-scoped intake metadata, evidence records, and intake audit events.
- Approval gates with atomic decisions and linked job transitions.
- Governed `metadata.analyze` jobs.
- Character count, whitespace-separated word count, and SHA-256 analysis.
- Stored responses for successful-job execution retries.
- Sanitized adapter failures and transactional job/audit persistence.
- Persisted typed workflow results and tenant-scoped result retrieval.
- A local operator CLI using the existing authenticated API.

This is not yet an AI summarization system, a file-preservation pipeline,
or a graphical application. An API storage reference does not prove that
a file has been copied or preserved.

## Local setup

Requires Docker Engine, Docker Compose v2, and Python 3.

Create a local `.env` using `.env.example`. Never commit credentials.

```bash
make up
```

The API and database bind to the local machine by default.
API documentation: `http://127.0.0.1:8000/docs`.

## Operator CLI

See `docs/operator-cli.md` for the governed workflow.

```bash
python3 scripts/atlas_cli.py --help
```

The CLI uses Python's standard library. It accepts UTF-8 working-copy text,
does not change that file, and requires approval by default.

## Verification

```bash
python3 -m unittest discover -s scripts -p 'test_atlas_cli.py' -v
make verify
```

`make verify` runs isolated PostgreSQL integration tests, the existing
containerized tests, readiness checks, and the live smoke workflow.

## Database migrations

Migrations `001` through `011` reside in
`services/atlas-control/migrations/`. Application startup applies pending
migrations transactionally before serving requests.

Migration `011` adds nullable persisted workflow results.
Historical jobs are not backfilled. The result endpoint returns HTTP 409
when a tenant-visible job has no persisted envelope.

## Safety and boundaries

- Preserve originals outside the repository; analyze a separate working copy.
- Keep tokens, personal data, and original research files out of Git.
- Current authentication uses development identities; do not assume
  production identity management is implemented.
- CLI approval commands remain subject to server permission checks.
- Shared correlation IDs are not a durable job-to-evidence relationship.
- Use `make down` to stop services while preserving local database data.
- Do not use `docker compose down -v` unless you intend to erase that data.

## Next milestones

Candidate follow-on work includes durable evidence-to-job linkage,
controlled file preservation and manifests, and a user-facing interface.
These are not implemented by the operator CLI.
