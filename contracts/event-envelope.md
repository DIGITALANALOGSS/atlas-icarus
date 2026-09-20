# Atlas Event Envelope Contract

Every Atlas event must contain:

- `event_id`: unique event identifier
- `event_type`: stable dot-separated type, such as `research.document.ingested`
- `occurred_at`: UTC ISO 8601 timestamp
- `producer`: service or component that emitted the event
- `correlation_id`: identifier tying related work together
- `schema_version`: version of the event schema
- `payload`: event-specific JSON object

Rules:

- Use UTC timestamps.
- Never include passwords, API keys, access tokens, or private keys.
- Do not place original research files directly inside an event.
- Store large files separately and reference them with a controlled identifier.
