BEGIN;

CREATE TABLE tenants (
  tenant_id UUID PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE CHECK (
    slug ~ '^[a-z0-9][a-z0-9-]{0,62}$'
  ),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO tenants (tenant_id, slug)
VALUES (
  '00000000-0000-0000-0000-000000000001',
  'legacy-default'
)
ON CONFLICT (tenant_id) DO NOTHING;

ALTER TABLE approval_gates
  ADD COLUMN tenant_id UUID;

ALTER TABLE jobs
  ADD COLUMN tenant_id UUID;

UPDATE approval_gates
SET tenant_id = '00000000-0000-0000-0000-000000000001'
WHERE tenant_id IS NULL;

UPDATE jobs
SET tenant_id = '00000000-0000-0000-0000-000000000001'
WHERE tenant_id IS NULL;

ALTER TABLE approval_gates
  ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE jobs
  ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE approval_gates
  ADD CONSTRAINT approval_gates_tenant_id_fkey
  FOREIGN KEY (tenant_id)
  REFERENCES tenants (tenant_id)
  ON DELETE RESTRICT;

ALTER TABLE jobs
  ADD CONSTRAINT jobs_tenant_id_fkey
  FOREIGN KEY (tenant_id)
  REFERENCES tenants (tenant_id)
  ON DELETE RESTRICT;

ALTER TABLE approval_gates
  ADD CONSTRAINT approval_gates_tenant_gate_id_key
  UNIQUE (tenant_id, gate_id);

ALTER TABLE jobs
  DROP CONSTRAINT jobs_approval_gate_id_fkey;

ALTER TABLE jobs
  ADD CONSTRAINT jobs_tenant_approval_gate_id_fkey
  FOREIGN KEY (tenant_id, approval_gate_id)
  REFERENCES approval_gates (tenant_id, gate_id)
  ON DELETE RESTRICT;

CREATE INDEX approval_gates_tenant_created_at_idx
  ON approval_gates (tenant_id, created_at DESC, gate_id DESC);

CREATE INDEX approval_gates_tenant_status_created_at_idx
  ON approval_gates (tenant_id, status, created_at DESC, gate_id DESC);

CREATE INDEX jobs_tenant_created_at_idx
  ON jobs (tenant_id, created_at DESC, job_id DESC);

CREATE INDEX jobs_tenant_approval_gate_id_idx
  ON jobs (tenant_id, approval_gate_id)
  WHERE approval_gate_id IS NOT NULL;

COMMIT;
