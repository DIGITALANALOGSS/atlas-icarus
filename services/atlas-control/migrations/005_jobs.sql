CREATE TABLE IF NOT EXISTS jobs (
  job_id UUID PRIMARY KEY,
  job_type TEXT NOT NULL CHECK (
    job_type ~ '^[a-z0-9]+(\.[a-z0-9_]+)+$'
  ),
  request_payload JSONB NOT NULL CHECK (
    jsonb_typeof(request_payload) = 'object'
  ),
  status TEXT NOT NULL CHECK (
    status IN (
      'pending_approval',
      'queued',
      'running',
      'succeeded',
      'failed',
      'rejected'
    )
  ),
  approval_required BOOLEAN NOT NULL DEFAULT FALSE,
  approval_gate_id UUID REFERENCES approval_gates(gate_id),
  correlation_id UUID NOT NULL,
  result_payload JSONB,
  error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,

  CHECK (
    (approval_required = FALSE AND approval_gate_id IS NULL)
    OR
    (approval_required = TRUE AND approval_gate_id IS NOT NULL)
  ),

  CHECK (
    (status = 'pending_approval' AND approval_required = TRUE)
    OR
    (status <> 'pending_approval')
  ),

  CHECK (
    (status IN ('succeeded', 'failed', 'rejected') AND completed_at IS NOT NULL)
    OR
    (status NOT IN ('succeeded', 'failed', 'rejected'))
  )
);

CREATE INDEX IF NOT EXISTS jobs_status_created_at_idx
  ON jobs (status, created_at DESC, job_id DESC);

CREATE INDEX IF NOT EXISTS jobs_correlation_id_idx
  ON jobs (correlation_id);

CREATE INDEX IF NOT EXISTS jobs_approval_gate_id_idx
  ON jobs (approval_gate_id)
  WHERE approval_gate_id IS NOT NULL;
