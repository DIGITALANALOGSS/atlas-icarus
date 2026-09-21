CREATE TABLE IF NOT EXISTS evidence_records (
  evidence_id UUID PRIMARY KEY,
  intake_id UUID NOT NULL REFERENCES intake_items (intake_id) ON DELETE RESTRICT,
  sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  storage_reference TEXT NOT NULL CHECK (btrim(storage_reference) <> ''),
  media_type TEXT CHECK (media_type IS NULL OR btrim(media_type) <> ''),
  filename TEXT CHECK (filename IS NULL OR btrim(filename) <> ''),
  description TEXT CHECK (description IS NULL OR char_length(description) <= 2000),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
  correlation_id UUID NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS evidence_records_intake_id_created_at_idx
  ON evidence_records (intake_id, created_at DESC);

CREATE INDEX IF NOT EXISTS evidence_records_sha256_idx
  ON evidence_records (sha256);

CREATE INDEX IF NOT EXISTS evidence_records_correlation_id_idx
  ON evidence_records (correlation_id);
