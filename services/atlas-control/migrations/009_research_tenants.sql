BEGIN;

ALTER TABLE intake_items
  ADD COLUMN tenant_id UUID;

ALTER TABLE evidence_records
  ADD COLUMN tenant_id UUID;

UPDATE intake_items
SET tenant_id = '11111111-1111-1111-1111-111111111111'
WHERE tenant_id IS NULL;

UPDATE evidence_records
SET tenant_id = '11111111-1111-1111-1111-111111111111'
WHERE tenant_id IS NULL;

ALTER TABLE intake_items
  ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE evidence_records
  ALTER COLUMN tenant_id SET NOT NULL;

ALTER TABLE intake_items
  ADD CONSTRAINT intake_items_tenant_id_fkey
  FOREIGN KEY (tenant_id)
  REFERENCES tenants (tenant_id)
  ON DELETE RESTRICT;

ALTER TABLE evidence_records
  ADD CONSTRAINT evidence_records_tenant_id_fkey
  FOREIGN KEY (tenant_id)
  REFERENCES tenants (tenant_id)
  ON DELETE RESTRICT;

ALTER TABLE intake_items
  ADD CONSTRAINT intake_items_tenant_intake_id_key
  UNIQUE (tenant_id, intake_id);

ALTER TABLE evidence_records
  DROP CONSTRAINT evidence_records_intake_id_fkey;

ALTER TABLE evidence_records
  ADD CONSTRAINT evidence_records_tenant_intake_id_fkey
  FOREIGN KEY (tenant_id, intake_id)
  REFERENCES intake_items (tenant_id, intake_id)
  ON DELETE RESTRICT;

CREATE INDEX intake_items_tenant_created_at_idx
  ON intake_items (tenant_id, created_at DESC, intake_id DESC);

CREATE INDEX intake_items_tenant_correlation_id_idx
  ON intake_items (tenant_id, correlation_id);

CREATE INDEX evidence_records_tenant_intake_created_at_idx
  ON evidence_records (
    tenant_id,
    intake_id,
    created_at DESC,
    evidence_id DESC
  );

CREATE INDEX evidence_records_tenant_evidence_id_idx
  ON evidence_records (tenant_id, evidence_id);

COMMIT;
