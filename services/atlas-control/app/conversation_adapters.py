# atlas-model-streaming-v1
from __future__ import annotations

import asyncio
import json
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    content: str


class ConversationModel(Protocol):
    async def complete(self, messages: Sequence[ConversationTurn]) -> str:
        """Return the assistant reply for the supplied ordered conversation."""


    async def stream(self, messages):
        """Yield assistant text fragments; callers must close on interruption."""
        ...



class EchoConversationModel:
    """Deterministic local implementation used until a model provider is configured."""

    async def complete(self, messages: Sequence[ConversationTurn]) -> str:
        for message in reversed(messages):
            if message.role == "user":
                return f"Atlas received: {message.content}"
        return "Atlas is ready."


    async def stream(self, messages):
        """Streaming-compatible deterministic implementation."""
        yield await self.complete(messages)



class ConversationModelError(RuntimeError):
    """Raised when a configured conversation model cannot produce a reply."""


@dataclass(frozen=True)
class LlamaCppConversationModel:
    """OpenAI-compatible adapter for a local llama.cpp server."""

    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 60.0
    max_tokens: int = 512
    temperature: float = 0.2
    stream_client: object | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ConversationModelError("ATLAS_MODEL_API_KEY is not configured")
        try:
            timeout = float(self.timeout_seconds)
        except (TypeError, ValueError) as error:
            raise ConversationModelError(
                "ATLAS_MODEL_TIMEOUT_SECONDS must be numeric"
            ) from error
        if not math.isfinite(timeout) or timeout <= 0:
            raise ConversationModelError(
                "ATLAS_MODEL_TIMEOUT_SECONDS must be finite and greater than zero"
            )
        object.__setattr__(self, "timeout_seconds", timeout)


    @classmethod
    def from_environment(cls) -> LlamaCppConversationModel:
        base_url = os.environ.get(
            "ATLAS_MODEL_BASE_URL",
            "http://127.0.0.1:8080/v1",
        )
        model = os.environ.get("ATLAS_MODEL_NAME", "atlas-qwen")
        api_key = os.environ.get("ATLAS_MODEL_API_KEY")
        if not api_key:
            raise ConversationModelError("ATLAS_MODEL_API_KEY is not configured")

        try:
            timeout_seconds = float(
                os.environ.get("ATLAS_MODEL_TIMEOUT_SECONDS", "60")
            )
        except ValueError as error:
            raise ConversationModelError(
                "ATLAS_MODEL_TIMEOUT_SECONDS must be numeric"
            ) from error

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ConversationModelError(
                "ATLAS_MODEL_TIMEOUT_SECONDS must be greater than zero"
            )

        return cls(
            base_url=base_url.rstrip("/"),
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )

    async def complete(self, messages: Sequence[ConversationTurn]) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        return await asyncio.to_thread(self._complete_sync, payload)

    def _complete_sync(self, payload: dict) -> str:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            f"{self.base_url.rstrip(chr(47))}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw_response = response.read()
        except HTTPError as error:
            raise ConversationModelError(
                f"model request failed with HTTP {error.code}"
            ) from error
        except URLError as error:
            raise ConversationModelError(
                "model request failed: network connection error"
            ) from error
        except OSError as error:
            raise ConversationModelError(
                "model request failed: transport error"
            ) from error

        try:
            decoded = json.loads(raw_response)
            content = decoded["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConversationModelError(
                "model response did not contain choices[0].message.content"
            ) from error

        if not isinstance(content, str) or not content.strip():
            raise ConversationModelError("model response content was blank")

        return content.strip()


    async def stream(self, messages):
        """Yield live text fragments without waiting for the full answer."""
        from contextlib import aclosing
        from app.conversation_streaming import stream_completion

        async with aclosing(stream_completion(self, messages)) as fragments:
            async for fragment in fragments:
                yield fragment
