"""Approval-required conversation proposals. Never executes jobs."""
import hashlib
from app.conversation_coordinator import ConversationCoordinatorError
from app.conversation_tool_calls import ModelToolCall, _load

def proposal_tools():
    return [{
        "type": "function",
        "function": {
            "name": "atlas_metadata_analyze",
            "description": "Prepare text metadata analysis for human approval; do not execute.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string", "minLength": 1, "maxLength": 20000,
                    },
                },
                "required": ["content"],
                "additionalProperties": False,
            },
        },
    }]

async def prepare_tool_job(connection, principal, turn, call):
    if "jobs:create" not in principal.permissions:
        raise ConversationCoordinatorError("permission denied")
    try:
        if (
            not isinstance(call, ModelToolCall)
            or call.name != "atlas_metadata_analyze"
            or not isinstance(call.id, str)
            or not call.id.strip() or len(call.id) > 256
            or not isinstance(call.arguments_json, str)
            or len(call.arguments_json) > 131072
        ):
            raise ValueError()
        arguments = _load(call.arguments_json)
        if not isinstance(arguments, dict) or set(arguments) != {"content"}:
            raise ValueError()
        content = arguments["content"]
        if not isinstance(content, str) or not content.strip() or len(content) > 20000:
            raise ValueError()
    except (ValueError, TypeError, RecursionError):
        raise ConversationCoordinatorError("invalid tool proposal") from None

    from app.main import JobCreate, _create_job_on_connection

    provenance = {
        "turn_id": str(turn["turn_id"]),
        "conversation_id": str(turn["conversation_id"]),
        "subject_id": principal.subject_id,
        "tool_call_id": call.id,
        "tool_name": call.name,
        "arguments_json": call.arguments_json,
        "arguments_sha256": hashlib.sha256(
            call.arguments_json.encode("utf-8")
        ).hexdigest(),
    }
    job = JobCreate(
        job_type="metadata.analyze",
        request_payload={"content": content, "_atlas_proposal": provenance},
        approval_required=True,
        correlation_id=turn["correlation_id"],
    )
    return await _create_job_on_connection(connection, job, principal)
