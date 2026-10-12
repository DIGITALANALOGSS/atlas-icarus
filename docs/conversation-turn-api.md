# Conversation turn API

Set ATLAS_CONVERSATION_MODEL explicitly to echo or llama_cpp before service startup.
llama_cpp uses the existing ATLAS_MODEL_* settings. There is no silent fallback.

POST /conversations/{conversation_id}/turns
Body: {"user_message_id": "<UUID>"}. Extra fields are rejected.
Executes synchronously against an already-persisted user message.
Requires conversations:read, conversations:write, and conversation ownership.
Completed requests replay the stored result without another model call.
Active, failed, and cancelled turns are not automatically regenerated.

GET /conversations/{conversation_id}/turns/{turn_id}
Requires conversations:read and ownership.

POST /conversations/{conversation_id}/turns/{turn_id}/cancel
No request body is required. Requires read/write permissions and ownership.
Cancellation is idempotent for cancelled turns; completed/failed turns conflict.

Paths bind conversation, turn, tenant, and owner. Public responses omit worker
tokens and lease details. Generation releases its context-loading connection.
Assistant insertion, completion, timestamp update, and audit are atomic.

Expired running turns become failed with lease_expired on execution, status
access, or cancellation in the owned conversation. No periodic worker,
heartbeat, automatic retry, or durable background dispatcher is provided.

HTTP: 200 success/replay; 401 unauthenticated; 403 insufficient permissions;
404 inaccessible resource; 409 lifecycle conflict/non-user source;
422 invalid input; 502 model error; 503 database/audit/unclassified execution
error; 504 execution timeout.

Lifecycle integration cases cover success/replay, model failure, cancellation,
expiry/stale publication, and publication audit rollback, plus request and
owner/path checks. The rollback harness uses one physical connection.
Separate-connection contention, cross-tenant cases, cancellation during model
execution, process interruption, and timeout still need dedicated coverage.

This patch does not restart services or migrate the application database.
Production activation requires a separate deployment/migration decision.
