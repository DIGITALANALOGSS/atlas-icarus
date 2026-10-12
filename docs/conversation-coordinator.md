# Conversation coordinator contract

Status: designed; not implemented or operationally verified.

## Scope
Connect authorized durable conversation messages to the model-provider boundary.
Preserve the wider Atlas voice, multimodal, memory, evidence, and governed-action architecture.
Do not execute external actions through this coordinator increment.

## Durable turn record
Store tenant, conversation, initiating user message, turn ID, correlation ID,
status, attempt, worker fencing token, lease expiry, timestamps, error code,
and resulting assistant-message reference.
Tenant-scoped foreign keys must bind messages and turns to the same conversation.
One initiating user message identifies one turn; conflicting replay is rejected.
Permit at most one queued or running turn per conversation in this increment.

## Execution
Authorize conversation read and write access and verify owner scope.
Reserve the turn transactionally and capture an ordered context boundary.
Invoke the model outside database transactions and conversation row locks.
Publish the assistant message and completed turn in one transaction.
Recheck ownership, running status, fencing token, and context validity at publication.
Do not publish a late result after cancellation or worker replacement.

## States and recovery
Queued -> running -> completed, failed, or cancelled.
Queued -> cancelled.
Persist cancellation; caller disconnection alone is not durable cancellation.
Expired workers lose publication authority.
Do not silently retry failed or expired turns; recovery policy must be explicit.
A cancelled blocking model worker may finish, but cannot publish its result.

## Acceptance gates
Unit and real PostgreSQL tests for replay, scope, concurrency, atomic publication,
cancellation races, fencing, lease expiry, failure, and recovery.
Real provider integration remains separate from deterministic adapter tests.
Streaming, voice transport, provider-side cancellation, and external actions
are not claimed implemented by this contract.
