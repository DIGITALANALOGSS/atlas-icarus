from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ConversationTurn:
    role: str
    content: str


class ConversationModel(Protocol):
    async def complete(self, messages: Sequence[ConversationTurn]) -> str:
        """Return the assistant reply for the supplied ordered conversation."""


class EchoConversationModel:
    """Deterministic local implementation used until a model provider is configured."""

    async def complete(self, messages: Sequence[ConversationTurn]) -> str:
        for message in reversed(messages):
            if message.role == "user":
                return f"Atlas received: {message.content}"
        return "Atlas is ready."
