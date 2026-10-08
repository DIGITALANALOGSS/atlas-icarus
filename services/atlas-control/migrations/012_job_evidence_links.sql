ALTER TABLE evidence_records
  ADD CONSTRAINT evidence_records_tenant_evidence_id_key
  UNIQUE (tenant_id, evidence_id);

ALTER TABLE jobs
  ADD COLUMN evidence_id UUID;

ALTER TABLE jobs
  ADD CONSTRAINT jobs_tenant_evidence_id_fkey
  FOREIGN KEY (tenant_id, evidence_id)
  REFERENCES evidence_records (tenant_id, evidence_id)
  ON DELETE RESTRICT;

CREATE INDEX jobs_tenant_evidence_id_idx
  ON jobs (tenant_id, evidence_id)
  WHERE evidence_id IS NOT NULL;
