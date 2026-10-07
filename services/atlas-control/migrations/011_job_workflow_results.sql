ALTER TABLE jobs
  ADD COLUMN workflow_result JSONB;

ALTER TABLE jobs
  ADD CONSTRAINT jobs_workflow_result_matches_job CHECK (
    workflow_result IS NULL
    OR (
      jsonb_typeof(workflow_result) = 'object'
      AND workflow_result ?& ARRAY[
        'result_id', 'node_id', 'workflow_id', 'correlation_id',
        'tenant_id', 'status', 'output', 'completed_at'
      ]
      AND (workflow_result->>'node_id') IS NOT DISTINCT FROM job_id::text
      AND (workflow_result->>'workflow_id') IS NOT DISTINCT FROM job_id::text
      AND (workflow_result->>'tenant_id') IS NOT DISTINCT FROM tenant_id::text
      AND (workflow_result->>'correlation_id')
            IS NOT DISTINCT FROM correlation_id::text
      AND (workflow_result->>'status') IS NOT DISTINCT FROM status
      AND status IN ('succeeded', 'failed')
    )
  );
