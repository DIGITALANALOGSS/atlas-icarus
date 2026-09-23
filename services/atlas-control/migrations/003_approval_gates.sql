CREATE TABLE IF NOT EXISTS approval_gates (
  gate_id UUID PRIMARY KEY,
  requester TEXT NOT NULL CHECK (btrim(requester) <> ''),
  action_type TEXT NOT NULL CHECK (
    action_type ~ '^[a-z0-9]+(\.[a-z0-9]+)+$'
  ),
  risk_level TEXT NOT NULL CHECK (
    risk_level IN ('low', 'medium', 'high', 'critical')
  ),
  action_payload JSONB NOT NULL CHECK (jsonb_typeof(action_payload) = 'object'),
  summary TEXT NOT NULL CHECK (
    btrim(summary) <> '' AND char_length(summary) <= 2000
  ),
  status TEXT NOT NULL CHECK (
    status IN ('pending', 'approved', 'rejected')
  ),
  correlation_id UUID NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  decided_at TIMESTAMPTZ,
  decided_by TEXT,
  decision_reason TEXT,
  decision_event_id UUID,
  CHECK (
    (status = 'pending'
      AND decided_at IS NULL
      AND decided_by IS NULL
      AND decision_reason IS NULL
      AND decision_event_id IS NULL)
    OR
    (status IN ('approved', 'rejected')
      AND decided_at IS NOT NULL
      AND decided_by IS NOT NULL
      AND decision_event_id IS NOT NULL)
  ),
  CHECK (
    decision_reason IS NULL OR char_length(decision_reason) <= 2000
  )
);

CREATE INDEX IF NOT EXISTS approval_gates_status_created_at_idx
  ON approval_gates (status, created_at DESC, gate_id DESC);

CREATE INDEX IF NOT EXISTS approval_gates_correlation_id_idx
  ON approval_gates (correlation_id);
