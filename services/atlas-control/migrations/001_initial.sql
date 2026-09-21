CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS intake_items (
  intake_id UUID PRIMARY KEY,
  source TEXT NOT NULL CHECK (btrim(source) <> ''),
  received_at TIMESTAMPTZ NOT NULL,
  custodian TEXT NOT NULL CHECK (btrim(custodian) <> ''),
  storage_reference TEXT NOT NULL CHECK (btrim(storage_reference) <> ''),
  original_sha256 CHAR(64) NOT NULL CHECK (original_sha256 ~ '^[0-9a-f]{64}$'),
  notes TEXT CHECK (notes IS NULL OR char_length(notes) <= 2000),
  correlation_id UUID NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS intake_items_created_at_idx
  ON intake_items (created_at DESC);

CREATE INDEX IF NOT EXISTS intake_items_correlation_id_idx
  ON intake_items (correlation_id);

CREATE TABLE IF NOT EXISTS events (
  event_id UUID PRIMARY KEY,
  event_type TEXT NOT NULL CHECK (event_type ~ '^[a-z0-9]+(\.[a-z0-9]+)+$'),
  occurred_at TIMESTAMPTZ NOT NULL,
  producer TEXT NOT NULL CHECK (btrim(producer) <> ''),
  correlation_id UUID NOT NULL,
  schema_version TEXT NOT NULL CHECK (btrim(schema_version) <> ''),
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS events_occurred_at_idx
  ON events (occurred_at DESC);

CREATE INDEX IF NOT EXISTS events_correlation_id_idx
  ON events (correlation_id);
