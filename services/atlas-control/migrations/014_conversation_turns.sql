ALTER TABLE conversation_messages
  ADD CONSTRAINT conversation_messages_tenant_conversation_message_key
  UNIQUE (tenant_id, conversation_id, message_id);

CREATE TABLE conversation_turns (
  turn_id UUID PRIMARY KEY,
  tenant_id UUID NOT NULL,
  conversation_id UUID NOT NULL,
  user_message_id UUID NOT NULL,
  assistant_message_id UUID,
  correlation_id UUID NOT NULL,
  context_sequence_number BIGINT NOT NULL CHECK (context_sequence_number >= 1),
  context_sha256 TEXT NOT NULL CHECK (context_sha256 ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL CHECK (
    status IN ('queued', 'running', 'completed', 'failed', 'cancelled')
  ),
  attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
  worker_token UUID,
  lease_expires_at TIMESTAMPTZ,
  error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  completed_at TIMESTAMPTZ,

  UNIQUE (tenant_id, conversation_id, turn_id),
  UNIQUE (tenant_id, conversation_id, user_message_id),
  UNIQUE (tenant_id, conversation_id, assistant_message_id),

  FOREIGN KEY (tenant_id, conversation_id)
    REFERENCES conversations (tenant_id, conversation_id) ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, conversation_id, user_message_id)
    REFERENCES conversation_messages (tenant_id, conversation_id, message_id)
    ON DELETE RESTRICT,
  FOREIGN KEY (tenant_id, conversation_id, assistant_message_id)
    REFERENCES conversation_messages (tenant_id, conversation_id, message_id)
    ON DELETE RESTRICT,

  CHECK (
    (status = 'running' AND worker_token IS NOT NULL
      AND lease_expires_at IS NOT NULL AND started_at IS NOT NULL AND attempt >= 1)
    OR
    (status <> 'running' AND worker_token IS NULL AND lease_expires_at IS NULL)
  ),
  CHECK (
    (status IN ('completed', 'failed', 'cancelled') AND completed_at IS NOT NULL)
    OR
    (status IN ('queued', 'running') AND completed_at IS NULL)
  ),
  CHECK (
    (status = 'completed' AND assistant_message_id IS NOT NULL)
    OR
    (status <> 'completed' AND assistant_message_id IS NULL)
  ),
  CHECK (
    (status = 'failed' AND error_code IS NOT NULL AND length(btrim(error_code)) > 0)
    OR
    (status <> 'failed' AND error_code IS NULL)
  ),
  CHECK (assistant_message_id IS NULL OR assistant_message_id <> user_message_id)
);

CREATE UNIQUE INDEX conversation_turns_one_active_per_conversation
  ON conversation_turns (tenant_id, conversation_id)
  WHERE status IN ('queued', 'running');

CREATE INDEX conversation_turns_expiring_lease_idx
  ON conversation_turns (lease_expires_at, turn_id)
  WHERE status = 'running';
