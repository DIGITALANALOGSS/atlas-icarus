# Atlas Service Health Contract

Every Atlas service must expose:

- `GET /healthz`
- HTTP `200` when its process is alive
- A JSON response with `status`, `service`, `version`, and `timestamp_utc`

Example:

{
  "status": "ok",
  "service": "research-ingest",
  "version": "0.1.0",
  "timestamp_utc": "2026-09-20T00:00:00Z"
}
