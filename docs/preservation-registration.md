# Registering preservation manifests

Registration is explicit and separate from preservation. It sends metadata
only; it does not upload file bytes, approve a gate, or create a job.

## Usage

Start the local service and configure an authorized token. For local
development only:

```bash
export ATLAS_TOKEN=dev-admin
python3 scripts/atlas_register.py /private/storage/UUID/manifest.json
```

The token requires intake-create and evidence-create permissions.
The base URL defaults to the local API. ATLAS_BASE_URL or --base-url can
override it under the operator CLI URL restrictions.

The tool validates the manifest layout and reverifies both copies against
the recorded size and SHA-256 before requests. Changed working copies must
not be registered against the old digest.

It creates an intake referencing the preserved copy, then evidence
referencing the working copy. Both share an explicit correlation ID.
The preservation manifest remains unchanged.

Local paths are metadata references; the server does not open those files.
Do not assume the references are portable to another host.

## Receipt and recovery

A separate registration.json is created before requests. It contains the
API origin, preservation ID, correlation ID, stage, and known identifiers.
It contains no bearer token.

Possible states include prepared, intake_request_pending,
intake_registered, evidence_request_pending, registered, and needs_review.

An existing receipt prevents another attempt, including attempts by a
different tenant or API origin. This is a local duplication guard, not
server-side idempotency or tenant identity verification.

The two API operations are not atomic together. A failed or interrupted
request may have committed remotely even when no identifier was received.
An intake can exist without its evidence record.

For incomplete attempts:
- Keep the receipt and preserved copies.
- Inspect the known intake and server audit/database records using
  authorized access, correlation ID, and recorded identifiers.
- Do not delete the receipt and retry blindly.
- Do not automatically delete server records as compensation.

No automatic resume or recovery is implemented in this milestone.
If saving a receipt fails, its last persisted stage may be earlier than the
actual server state. Treat pending or ambiguous states as needing review.

## Linked analysis

After successful registration, use the returned evidence_id explicitly:

```bash
python3 scripts/atlas_cli.py submit /private/storage/UUID/working/content.bin --evidence-id EVIDENCE_UUID
```

Text analysis still requires nonblank UTF-8 content within the input limit.
Binary preservation/registration does not imply text-analysis eligibility.

## Security boundaries

Use trusted local storage without concurrent writers. Reverification is
not a defense against all filesystem races or a maliciously fabricated
manifest. Paths and metadata may be sensitive and are sent to the API.
No source authenticity, immutable storage, or complete chain of custody
is established by this operation.

## Tests

```bash
python3 -m unittest discover -s scripts -p 'test_atlas_*.py' -v
```

Tests use synthetic temporary copies and fake API clients only.
