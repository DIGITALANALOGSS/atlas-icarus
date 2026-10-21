-- Extracted proposals are not automatically approved memories.
CREATE TABLE conversation_memory_proposals (
  proposal_id UUID PRIMARY KEY,
  tenant_id UUID NOT NULL,
  conversation_id UUID NOT NULL,
  source_message_id UUID NOT NULL,
  job_id UUID NOT NULL,
  category TEXT NOT NULL CHECK (category IN (
    'communication_preference', 'accessibility_preference',
    'personal_fact', 'project_decision', 'goal', 'constraint'
  )),
  content TEXT NOT NULL CHECK (
    length(content) BETWEEN 1 AND 1000
    AND length(btrim(content)) > 0
  ),
  status TEXT NOT NULL DEFAULT 'proposed'
    CHECK (status IN ('proposed', 'accepted', 'rejected')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  reviewed_at TIMESTAMPTZ,
  reviewed_by_subject_id TEXT,

  CONSTRAINT memory_proposals_job_fkey
    FOREIGN KEY (
      tenant_id, conversation_id, source_message_id, job_id
    )
    REFERENCES conversation_learning_jobs (
      tenant_id, conversation_id, source_message_id, job_id
    )
    ON DELETE RESTRICT,

  CONSTRAINT memory_proposals_source_content_key
    UNIQUE (
      tenant_id, conversation_id, source_message_id, category, content
    ),

  CONSTRAINT memory_proposals_review_check CHECK (
    (status = 'proposed'
      AND reviewed_at IS NULL AND reviewed_by_subject_id IS NULL)
    OR
    (status IN ('accepted', 'rejected')
      AND reviewed_at IS NOT NULL
      AND reviewed_by_subject_id IS NOT NULL
      AND length(btrim(reviewed_by_subject_id)) > 0)
  )
);

CREATE INDEX conversation_memory_proposals_review_idx
ON conversation_memory_proposals (
  tenant_id, conversation_id, status, created_at, proposal_id
);
