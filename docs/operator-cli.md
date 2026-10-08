# Local operator CLI

Run from the repository root with Atlas Control available locally.

## Configure authentication

For local development only:

```bash
export ATLAS_TOKEN=dev-admin
```

Do not place real tokens in committed files or pass them as command-line
arguments. Environment variables are not a secure credential vault.

The default API origin is `http://127.0.0.1:8000`.
Override with `ATLAS_BASE_URL` or the global `--base-url` option.
Non-loopback origins require HTTPS. Credentials in URLs are rejected.

## Submit a working copy

```bash
python3 scripts/atlas_cli.py submit /path/to/working-copy.txt
```

Submission requires approval by default. It does not approve or execute
the job automatically. Input must be nonblank UTF-8 text, at most 20,000
characters. The file is read but not modified or preserved by this tool.

Record the returned `job_id`.

To explicitly create an ungated job:

```bash
python3 scripts/atlas_cli.py submit /path/to/working-copy.txt --ungated
```

An optional `--correlation-id UUID` sets correlation metadata. It does not
create a durable relationship to an intake or evidence record.

## Inspect and decide

Replace `JOB_UUID` with the returned identifier:

```bash
python3 scripts/atlas_cli.py show JOB_UUID
python3 scripts/atlas_cli.py approve JOB_UUID --decided-by operator --reason "Reviewed working copy"
```

Alternatively, reject:

```bash
python3 scripts/atlas_cli.py reject JOB_UUID --decided-by operator --reason "Do not execute"
```

Decisions resolve the linked gate from the job. The token must have both
job-read and gate-decision permissions. `decided-by` is a submitted label;
it does not change the authenticated principal.

## Execute and retrieve the result

```bash
python3 scripts/atlas_cli.py execute JOB_UUID
python3 scripts/atlas_cli.py result JOB_UUID
```

Execution remains governed by the backend. There are no automatic retries
or automatic decisions. A successful execution retry returns the stored
job response. A rejected or failed job is not automatically restarted.

The result command returns the persisted typed envelope. HTTP 409 can mean
that a result is not available; the CLI does not manufacture an envelope.

Successful HTTP responses print JSON. Inspect the returned job status:
HTTP success may carry a terminal failed job. Transport/API errors print
a sanitized message to stderr and exit nonzero.

Text, job responses, and outputs may be sensitive. Do not paste them into
public repositories or logs without reviewing their contents.

## CLI tests

```bash
python3 -m unittest discover -s scripts -p 'test_atlas_cli.py' -v
```

These use fake HTTP responses and temporary files; they do not contact the
running service or database.

## Evidence-linked submission

```bash
python3 scripts/atlas_cli.py submit /path/to/working-copy.txt --evidence-id EVIDENCE_UUID
```

The evidence must exist in the authenticated tenant. The backend compares
the SHA-256 of the submitted UTF-8 content with the evidence digest.
The CLI preserves line endings when reading the text file.

Linked jobs inherit the evidence correlation ID. An explicitly supplied,
different correlation ID is rejected. Digest and correlation mismatches
return HTTP 409; unavailable evidence returns HTTP 404.

The job response includes `evidence_id`. A composite database foreign key
enforces same-tenant linkage and prevents deleting referenced evidence.
Unlinked submissions remain supported.

This verifies correspondence to registered metadata, not trustworthy
collection of the original. It does not copy files or fetch storage URLs.
