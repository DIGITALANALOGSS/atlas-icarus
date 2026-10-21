
"""atlas-model-streaming-v1: asynchronous OpenAI-compatible SSE."""
import asyncio
import json


def decode_event(data):
    """Return (finished, text) for one complete SSE data event."""
    if data.strip() == "[DONE]":
        return True, ""
    try:
        event = json.loads(data)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid model stream JSON") from exc

    if not isinstance(event, dict) or "error" in event:
        raise ValueError("model stream returned an error")

    choices = event.get("choices")
    if not isinstance(choices, list):
        raise ValueError("model stream choices unavailable")
    if not choices:
        return False, ""

    choice = choices[0]
    if not isinstance(choice, dict):
        raise ValueError("invalid model stream choice")

    delta = choice.get("delta", {})
    if not isinstance(delta, dict):
        raise ValueError("invalid model stream delta")
    text = delta.get("content")
    if text is None:
        return False, ""
    if not isinstance(text, str):
        raise ValueError("model stream content must be text")
    return False, text


async def stream_completion(model, messages):
    """Yield text fragments; closing/cancelling the generator closes HTTP."""
    import httpx
    from app.conversation_adapters import ConversationModelError

    payload = {
        "model": model.model,
        "messages": [
            {"role": item.role, "content": item.content}
            for item in messages
        ],
        "temperature": model.temperature,
        "max_tokens": model.max_tokens,
        "stream": True,
    }
    headers = {
        "Authorization": "Bearer " + model.api_key,
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }
    url = model.base_url.rstrip("/") + "/chat/completions"
    saw_text = False
    emitted = 0

    try:
        async with asyncio.timeout(model.timeout_seconds):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(model.timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                async with client.stream(
                    "POST", url, json=payload, headers=headers
                ) as response:
                    if response.status_code != 200:
                        raise ConversationModelError(
                            "model stream failed with HTTP "
                            + str(response.status_code)
                        )
                    content_type = response.headers.get(
                        "content-type", ""
                    ).lower()
                    if "text/event-stream" not in content_type:
                        raise ConversationModelError(
                            "model server did not return an SSE stream"
                        )

                    event_lines = []
                    event_size = 0
                    async for line in response.aiter_lines():
                        if len(line) > 65536:
                            raise ValueError("model stream line too large")
                        if line == "":
                            if not event_lines:
                                continue
                            finished, text = decode_event(
                                "\n".join(event_lines)
                            )
                            event_lines = []
                            event_size = 0
                            if text:
                                emitted += len(text)
                                if emitted > 200000:
                                    raise ValueError(
                                        "model stream output limit exceeded"
                                    )
                                saw_text = saw_text or bool(text.strip())
                                yield text
                            if finished:
                                if not saw_text:
                                    raise ValueError("model stream was blank")
                                return
                        elif line.startswith("data:"):
                            value = line[5:]
                            if value.startswith(" "):
                                value = value[1:]
                            event_size += len(value)
                            if event_size > 65536:
                                raise ValueError("model stream event too large")
                            event_lines.append(value)

                    if event_lines:
                        finished, text = decode_event("\n".join(event_lines))
                        if text:
                            emitted += len(text)
                            if emitted > 200000:
                                raise ValueError(
                                    "model stream output limit exceeded"
                                )
                            saw_text = saw_text or bool(text.strip())
                            yield text
                        if finished and saw_text:
                            return

                    raise ConversationModelError(
                        "model stream ended without a completion marker"
                    )
    except asyncio.CancelledError:
        raise
    except TimeoutError as exc:
        raise ConversationModelError(
            "model stream timed out"
        ) from exc
    except httpx.HTTPError as exc:
        raise ConversationModelError(
            "model stream transport failed"
        ) from exc
    except ValueError as exc:
        raise ConversationModelError(str(exc)) from exc
