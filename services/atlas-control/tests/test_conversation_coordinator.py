from pathlib import Path
import sys
from unittest.mock import Mock
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth import Principal
from app.conversation_coordinator import ConversationCoordinatorError, claim_turn


@pytest.mark.asyncio
@pytest.mark.parametrize("permissions", [
    frozenset(), frozenset({"conversations:read"}), frozenset({"conversations:write"}),
])
async def test_claim_requires_both_permissions_before_database(permissions):
    pool = Mock()
    principal = Principal("test-owner", uuid4(), permissions, "user")
    with pytest.raises(ConversationCoordinatorError, match="permission denied"):
        await claim_turn(pool, principal, uuid4(), write_event=Mock())
    pool.acquire.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("lease", [0, -1, 601, True, "90", None])
async def test_claim_rejects_invalid_lease_before_database(lease):
    pool = Mock()
    principal = Principal(
        "test-owner", uuid4(),
        frozenset({"conversations:read", "conversations:write"}), "user",
    )
    with pytest.raises(ConversationCoordinatorError, match="lease must"):
        await claim_turn(pool, principal, uuid4(), write_event=Mock(), lease_seconds=lease)
    pool.acquire.assert_not_called()
