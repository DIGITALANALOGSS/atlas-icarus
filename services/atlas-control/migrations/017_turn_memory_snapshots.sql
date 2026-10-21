-- Execution snapshots, not an authoritative memory store.
-- Historical rows retain NULL pairs; their original memory is unknown.
ALTER TABLE conversation_turns
  ADD COLUMN memory_context JSONB,
  ADD COLUMN memory_context_sha256 TEXT,
  ADD CONSTRAINT conversation_turns_memory_snapshot_check CHECK (
    (memory_context IS NULL AND memory_context_sha256 IS NULL)
    OR
    CASE
      WHEN jsonb_typeof(memory_context) = 'array' THEN
        jsonb_array_length(memory_context) <= 12
        AND memory_context_sha256 IS NOT NULL
        AND memory_context_sha256 ~ '^[0-9a-f]{64}$'
      ELSE FALSE
    END
  );
