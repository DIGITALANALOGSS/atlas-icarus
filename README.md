# Atlas ICARUS

Atlas ICARUS is a local-first platform foundation. The current implementation includes an Atlas Control FastAPI service, PostgreSQL persistence, intake-item evidence records, Docker Compose local development, and automated endpoint tests.

## Current service

`services/atlas-control` provides the Atlas Control API.

The local Compose stack includes:

- `postgres`: PostgreSQL 16 database with persistent Docker volume storage
- `atlas-control`: FastAPI application
- `atlas-control-tests`: isolated test-runner service enabled only through the `test` Compose profile

Both database and API ports bind to `127.0.0.1`, keeping them available only on the local machine by default.

## Prerequisites

- Docker Engine with Docker Compose v2
- A `.env` file at the repository root containing:

```dotenv
POSTGRES_DB=atlas
POSTGRES_USER=atlas
POSTGRES_PASSWORD=replace-with-a-long-local-password
POSTGRES_PORT=5432
```

Do not commit `.env` files or credentials.

## Start the local service

Build and start Atlas Control and its PostgreSQL dependency:

```bash
docker compose -f compose.yaml up -d --build atlas-control
```

The API is available locally at:

```text
http://127.0.0.1:8000
```

FastAPI interactive documentation is available at:

```text
http://127.0.0.1:8000/docs
```

## Run tests

Run the isolated Atlas Control test container:

```bash
docker compose -f compose.yaml --profile test run --rm --build atlas-control-tests
```

The evidence-record suite currently verifies:

- Successful evidence-record retrieval and response serialization
- `404` behavior for missing evidence records
- UUID validation before database access
- `503` behavior for database query failures

## Current evidence API

### Get one evidence record

```text
GET /evidence-records/{evidence_id}
```

The endpoint returns a serialized evidence record, including its intake identifier, SHA-256 digest, controlled storage reference, metadata, correlation identifier, and creation timestamp.

Example:

```bash
curl http://127.0.0.1:8000/evidence-records/YOUR-EVIDENCE-UUID
```

## Database migrations

Atlas Control migrations are stored in:

```text
services/atlas-control/migrations/
```

Current migrations:

- `001_initial.sql`: initial intake-item schema
- `002_evidence_records.sql`: evidence-record table and indexes

The evidence table indexes intake records by descending creation time and also indexes SHA-256 and correlation identifiers.

## Useful commands

Check repository state:

```bash
git status --short
```

Review service logs:

```bash
docker compose -f compose.yaml logs -f atlas-control
```

Stop local containers while preserving the PostgreSQL volume:

```bash
docker compose -f compose.yaml down
```

Stop containers and delete local PostgreSQL data:

```bash
docker compose -f compose.yaml down -v
```

Use `down -v` only when you intentionally want to erase local development data.

## Next planned capability

The next API capability is an intake-scoped, read-only evidence list endpoint with validated pagination and automated tests.
