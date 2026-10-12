from pathlib import Path
import sys

import pytest

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from app.conversation_adapters import ConversationTurn, EchoConversationModel


@pytest.mark.asyncio
async def test_echo_model_returns_latest_user_turn() -> None:
    model = EchoConversationModel()

    reply = await model.complete(
        [
            ConversationTurn(role="user", content="First request"),
            ConversationTurn(role="assistant", content="First reply"),
            ConversationTurn(role="user", content="Latest request"),
        ]
    )

    assert reply == "Atlas received: Latest request"


@pytest.mark.asyncio
async def test_echo_model_handles_empty_conversation() -> None:
    model = EchoConversationModel()

    reply = await model.complete([])

    assert reply == "Atlas is ready."


@pytest.mark.asyncio
async def test_echo_model_ignores_non_user_turns() -> None:
    model = EchoConversationModel()

    reply = await model.complete(
        [ConversationTurn(role="assistant", content="Prior reply")]
    )

    assert reply == "Atlas is ready."
