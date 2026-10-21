-- Durable learning queue; no worker is activated by this migration.
CREATE UNIQUE INDEX conversation_messages_learning_source_key
ON conversation_messages (tenant_id, conversation_id, message_id);

CREATE TABLE conversation_learning_jobs (
  job_id UUID PRIMARY KEY,
  tenant_id UUID NOT NULL,
  conversation_id UUID NOT NULL,
  source_message_id UUID NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'completed', 'failed')),
  attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  worker_token UUID,
  lease_expires_at TIMESTAMPTZ,
  available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  error_code TEXT,

  CONSTRAINT learning_jobs_source_fkey
    FOREIGN KEY (tenant_id, conversation_id, source_message_id)
    REFERENCES conversation_messages
      (tenant_id, conversation_id, message_id)
    ON DELETE RESTRICT,

  CONSTRAINT learning_jobs_one_per_source
    UNIQUE (tenant_id, conversation_id, source_message_id),

  CONSTRAINT learning_jobs_scoped_id_key
    UNIQUE (tenant_id, conversation_id, source_message_id, job_id),

  CONSTRAINT learning_jobs_lease_check CHECK (
    (status = 'running'
      AND worker_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    OR
    (status <> 'running'
      AND worker_token IS NULL AND lease_expires_at IS NULL)
  ),

  CONSTRAINT learning_jobs_completion_check CHECK (
    (status = 'completed' AND completed_at IS NOT NULL)
    OR (status <> 'completed' AND completed_at IS NULL)
  )
);

CREATE INDEX conversation_learning_jobs_pending_idx
ON conversation_learning_jobs (available_at, created_at, job_id)
WHERE status = 'pending';

CREATE INDEX conversation_learning_jobs_expired_idx
ON conversation_learning_jobs (lease_expires_at, job_id)
WHERE status = 'running';
