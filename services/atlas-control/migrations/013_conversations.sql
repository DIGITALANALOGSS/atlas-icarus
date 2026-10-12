CREATE TABLE conversations (
  conversation_id UUID PRIMARY KEY,
  tenant_id UUID NOT NULL,
  owner_subject_id TEXT NOT NULL CHECK (length(btrim(owner_subject_id)) > 0),
  title TEXT CHECK (
    title IS NULL OR (
      length(title) <= 500
      AND length(btrim(title)) > 0
    )
  ),
  correlation_id UUID NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT conversations_tenant_id_fkey
    FOREIGN KEY (tenant_id)
    REFERENCES tenants (tenant_id)
    ON DELETE RESTRICT,

  CONSTRAINT conversations_tenant_conversation_id_key
    UNIQUE (tenant_id, conversation_id)
);

CREATE TABLE conversation_messages (
  message_id UUID PRIMARY KEY,
  tenant_id UUID NOT NULL,
  conversation_id UUID NOT NULL,
  sequence_number BIGINT NOT NULL CHECK (sequence_number >= 1),
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content TEXT NOT NULL CHECK (
    length(content) <= 20000
    AND length(btrim(content)) > 0
  ),
  client_message_id UUID,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  CONSTRAINT conversation_messages_tenant_id_fkey
    FOREIGN KEY (tenant_id)
    REFERENCES tenants (tenant_id)
    ON DELETE RESTRICT,

  CONSTRAINT conversation_messages_tenant_conversation_id_fkey
    FOREIGN KEY (tenant_id, conversation_id)
    REFERENCES conversations (tenant_id, conversation_id)
    ON DELETE RESTRICT,

  CONSTRAINT conversation_messages_tenant_conversation_sequence_key
    UNIQUE (tenant_id, conversation_id, sequence_number)
);

CREATE UNIQUE INDEX conversation_messages_tenant_conversation_client_message_id_key
  ON conversation_messages (tenant_id, conversation_id, client_message_id)
  WHERE client_message_id IS NOT NULL;

CREATE INDEX conversations_tenant_owner_updated_at_idx
  ON conversations (
    tenant_id,
    owner_subject_id,
    updated_at DESC,
    conversation_id DESC
  );

CREATE INDEX conversation_messages_tenant_conversation_sequence_idx
  ON conversation_messages (
    tenant_id,
    conversation_id,
    sequence_number ASC,
    message_id ASC
  );
