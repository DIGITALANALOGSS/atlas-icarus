"""Opt-in non-streaming structured replies. Never executes tools."""
import asyncio
import json
from dataclasses import dataclass
import httpx
from app.conversation_adapters import (
    LlamaCppConversationModel, ConversationModelError,
)

@dataclass(frozen=True)
class ModelToolCall:
    id: str
    name: str
    arguments_json: str

    def arguments(self):
        return json.loads(self.arguments_json)

@dataclass(frozen=True)
class StructuredModelReply:
    content: str
    tool_calls: tuple
    finish_reason: str

def _bad_constant(value):
    raise ValueError("non-finite JSON")

def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result

def _load(raw):
    return json.loads(
        raw, parse_constant=_bad_constant,
        object_pairs_hook=_unique_object,
    )

def parse_reply(decoded, allowed_names):
    try:
        choices = decoded["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError()
        choice = choices[0]
        message = choice["message"]
        if message.get("role") != "assistant":
            raise ValueError()
        content = message.get("content")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise ValueError()
        raw_calls = message.get("tool_calls", [])
        if not isinstance(raw_calls, list) or len(raw_calls) > 32:
            raise ValueError()
        calls, ids = [], set()
        for call in raw_calls:
            ident = call["id"]
            function = call["function"]
            name = function["name"]
            arguments = function["arguments"]
            if (
                call.get("type") != "function"
                or not isinstance(ident, str) or not ident.strip()
                or ident in ids
                or not isinstance(name, str) or name not in allowed_names
                or not isinstance(arguments, str)
                or len(arguments) > 131072
            ):
                raise ValueError()
            if not isinstance(_load(arguments), dict):
                raise ValueError()
            ids.add(ident)
            calls.append(ModelToolCall(ident, name, arguments))
        reason = choice.get("finish_reason")
        if calls:
            if reason != "tool_calls":
                raise ValueError()
        elif reason != "stop" or not content.strip():
            raise ValueError()
        return StructuredModelReply(content, tuple(calls), reason)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
        raise ConversationModelError(
            "invalid structured model response"
        ) from None

class ToolCapableConversationModel(LlamaCppConversationModel):
    """Existing complete() and stream() remain inherited unchanged."""

    async def complete_with_tools(self, messages, *, tools):
        try:
            definitions = json.loads(json.dumps(tools, allow_nan=False))
            if not isinstance(definitions, list) or not definitions:
                raise ValueError()
            names = set()
            for tool in definitions:
                function = tool["function"]
                name = function["name"]
                if (
                    tool.get("type") != "function"
                    or not isinstance(name, str) or not name.strip()
                    or name in names
                    or not isinstance(function.get("parameters"), dict)
                ):
                    raise ValueError()
                names.add(name)
            payload = {
                "model": self.model,
                "messages": [
                    {"role": m.role, "content": m.content} for m in messages
                ],
                "tools": definitions,
                "tool_choice": "auto",
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "stream": False,
            }
            json.dumps(payload, allow_nan=False)
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            raise ConversationModelError(
                "invalid structured model request"
            ) from None

        async def request():
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    self.base_url.rstrip("/") + "/chat/completions",
                    json=payload,
                    headers={"Authorization": "Bearer " + self.api_key},
                )
                if response.status_code != 200:
                    raise ConversationModelError(
                        "structured model request failed with HTTP "
                        + str(response.status_code)
                    )
                if len(response.content) > 1048576:
                    raise ConversationModelError(
                        "structured model response too large"
                    )
                try:
                    decoded = _load(response.content)
                except (ValueError, UnicodeError, RecursionError):
                    raise ConversationModelError(
                        "invalid structured model JSON"
                    ) from None
                return parse_reply(decoded, names)

        try:
            return await asyncio.wait_for(
                request(), timeout=self.timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise ConversationModelError(
                "structured model request timed out"
            ) from None
        except httpx.HTTPError:
            raise ConversationModelError(
                "structured model transport failed"
            ) from None
