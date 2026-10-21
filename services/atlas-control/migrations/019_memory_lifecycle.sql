-- Acceptance history and current memory eligibility are separate.
ALTER TABLE conversation_memory_proposals
  ADD CONSTRAINT memory_proposals_tenant_proposal_key
    UNIQUE (tenant_id, proposal_id),
  ADD COLUMN lifecycle_state TEXT NOT NULL DEFAULT 'active',
  ADD COLUMN lifecycle_changed_at TIMESTAMPTZ,
  ADD COLUMN lifecycle_changed_by_subject_id TEXT,
  ADD COLUMN superseded_by_proposal_id UUID,
  ADD CONSTRAINT memory_proposals_lifecycle_check CHECK (
    (lifecycle_state = 'active'
      AND lifecycle_changed_at IS NULL
      AND lifecycle_changed_by_subject_id IS NULL
      AND superseded_by_proposal_id IS NULL)
    OR
    (lifecycle_state IN ('revoked', 'superseded')
      AND status = 'accepted'
      AND lifecycle_changed_at IS NOT NULL
      AND lifecycle_changed_by_subject_id IS NOT NULL
      AND length(btrim(lifecycle_changed_by_subject_id)) > 0
      AND (
        (lifecycle_state = 'revoked' AND superseded_by_proposal_id IS NULL)
        OR
        (lifecycle_state = 'superseded'
          AND superseded_by_proposal_id IS NOT NULL
          AND superseded_by_proposal_id <> proposal_id)
      ))
  ),
  ADD CONSTRAINT memory_proposals_replacement_fkey
    FOREIGN KEY (tenant_id, superseded_by_proposal_id)
    REFERENCES conversation_memory_proposals (tenant_id, proposal_id)
    ON DELETE RESTRICT;

CREATE TABLE conversation_memory_transitions (
  transition_id UUID PRIMARY KEY,
  tenant_id UUID NOT NULL,
  proposal_id UUID NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('revoked', 'superseded')),
  replacement_proposal_id UUID,
  actor_subject_id TEXT NOT NULL CHECK (length(btrim(actor_subject_id)) > 0),
  reason TEXT NOT NULL CHECK (
    length(reason) BETWEEN 1 AND 1000 AND length(btrim(reason)) > 0
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, proposal_id),
  FOREIGN KEY (tenant_id, proposal_id)
    REFERENCES conversation_memory_proposals (tenant_id, proposal_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, replacement_proposal_id)
    REFERENCES conversation_memory_proposals (tenant_id, proposal_id)
    ON DELETE RESTRICT,
  CHECK (
    (action = 'revoked' AND replacement_proposal_id IS NULL)
    OR
    (action = 'superseded' AND replacement_proposal_id IS NOT NULL
      AND replacement_proposal_id <> proposal_id)
  )
);

ALTER TABLE conversation_turns
  ADD COLUMN memory_dependencies JSONB,
  ADD COLUMN memory_dependencies_sha256 TEXT,
  ADD CONSTRAINT conversation_turns_memory_dependencies_check CHECK (
    (memory_dependencies IS NULL AND memory_dependencies_sha256 IS NULL)
    OR
    CASE WHEN jsonb_typeof(memory_dependencies) = 'array' THEN
      jsonb_array_length(memory_dependencies) <= 12
      AND memory_dependencies_sha256 IS NOT NULL
      AND memory_dependencies_sha256 ~ '^[0-9a-f]{64}$'
    ELSE FALSE END
  );
