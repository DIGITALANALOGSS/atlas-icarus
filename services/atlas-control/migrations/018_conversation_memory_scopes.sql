-- Bind backend conversations to existing workspace project identities.
-- This is not a separate project catalog or authoritative memory store.
CREATE TABLE conversation_workspace_bindings (
  tenant_id UUID NOT NULL,
  conversation_id UUID NOT NULL,
  scope_type TEXT NOT NULL
    CHECK (scope_type IN ('personal', 'project')),
  workspace_project_id UUID,
  bound_by_subject_id TEXT NOT NULL
    CHECK (length(btrim(bound_by_subject_id)) > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

  PRIMARY KEY (tenant_id, conversation_id),

  UNIQUE (tenant_id, conversation_id, workspace_project_id),

  FOREIGN KEY (tenant_id, conversation_id)
    REFERENCES conversations (tenant_id, conversation_id)
    ON DELETE RESTRICT,

  CHECK (
    (scope_type = 'personal' AND workspace_project_id IS NULL)
    OR
    (scope_type = 'project' AND workspace_project_id IS NOT NULL)
  )
);

-- Existing proposals remain source-conversation-only.
-- No acceptance, global promotion, or scope inference occurs here.
ALTER TABLE conversation_memory_proposals
  ADD COLUMN memory_scope TEXT NOT NULL DEFAULT 'conversation',
  ADD COLUMN workspace_project_id UUID,

  ADD CONSTRAINT conversation_memory_proposals_scope_check CHECK (
    (
      memory_scope = 'conversation'
      AND workspace_project_id IS NULL
    )
    OR
    (
      status = 'accepted'
      AND memory_scope = 'personal'
      AND workspace_project_id IS NULL
    )
    OR
    (
      status = 'accepted'
      AND memory_scope = 'project'
      AND workspace_project_id IS NOT NULL
    )
  ),

  ADD CONSTRAINT conversation_memory_proposals_project_binding_fkey
    FOREIGN KEY (tenant_id, conversation_id, workspace_project_id)
    REFERENCES conversation_workspace_bindings (
      tenant_id, conversation_id, workspace_project_id
    )
    ON DELETE RESTRICT
    ON UPDATE RESTRICT;

CREATE INDEX conversation_memory_proposals_accepted_scope_idx
ON conversation_memory_proposals (
  tenant_id, memory_scope, workspace_project_id, reviewed_at, proposal_id
)
WHERE status = 'accepted';
