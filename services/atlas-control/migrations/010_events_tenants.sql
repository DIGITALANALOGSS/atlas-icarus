BEGIN;

ALTER TABLE events
  ADD COLUMN tenant_id UUID;

DO $$
BEGIN
  IF EXISTS (
    WITH tenant_candidates AS (
      SELECT correlation_id, tenant_id FROM intake_items
      UNION
      SELECT correlation_id, tenant_id FROM approval_gates
      UNION
      SELECT correlation_id, tenant_id FROM jobs
    ),
    resolved_events AS (
      SELECT
        e.event_id,
        COUNT(DISTINCT tc.tenant_id) AS tenant_count
      FROM events AS e
      LEFT JOIN tenant_candidates AS tc
        ON tc.correlation_id = e.correlation_id
      GROUP BY e.event_id
    )
    SELECT 1
    FROM resolved_events
    WHERE tenant_count <> 1
  ) THEN
    RAISE EXCEPTION
      'cannot backfill events.tenant_id: unresolved or ambiguous event ownership';
  END IF;
END
$$;

WITH tenant_candidates AS (
  SELECT correlation_id, tenant_id FROM intake_items
  UNION
  SELECT correlation_id, tenant_id FROM approval_gates
  UNION
  SELECT correlation_id, tenant_id FROM jobs
),
resolved_events AS (
  SELECT
    e.event_id,
    (ARRAY_AGG(DISTINCT tc.tenant_id))[1] AS tenant_id
  FROM events AS e
  JOIN tenant_candidates AS tc
    ON tc.correlation_id = e.correlation_id
  GROUP BY e.event_id
)
UPDATE events AS e
SET tenant_id = r.tenant_id
FROM resolved_events AS r
WHERE e.event_id = r.event_id
  AND e.tenant_id IS NULL;

ALTER TABLE events
  ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE events
  ADD CONSTRAINT events_tenant_id_fkey
  FOREIGN KEY (tenant_id)
  REFERENCES tenants (tenant_id)
  ON DELETE RESTRICT;

CREATE INDEX events_tenant_correlation_occurred_at_idx
  ON events (
    tenant_id,
    correlation_id,
    occurred_at ASC,
    event_id ASC
  );

COMMIT;
