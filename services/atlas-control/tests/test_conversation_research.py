from unittest.mock import AsyncMock

import pytest

from app import conversation_research as research
from app.conversation_search import SearchError


@pytest.mark.asyncio
async def test_disabled_research_never_searches(monkeypatch):
    monkeypatch.delenv("ATLAS_RESEARCH_ENABLED", raising=False)
    search = AsyncMock()
    monkeypatch.setattr(research, "search_web", search)
    with pytest.raises(SearchError, match="disabled"):
        await research.research_answer(AsyncMock(), "test", remaining=10)
    search.assert_not_awaited()


@pytest.mark.asyncio
async def test_research_uses_only_query_and_evidence(monkeypatch):
    monkeypatch.setenv("ATLAS_RESEARCH_ENABLED", "1")
    monkeypatch.setenv("ATLAS_SEARCH_BASE_URL", "http://search:8080")
    search = AsyncMock(return_value={"results": [{
        "source_id": 1,
        "title": "Example",
        "url": "https://example.org/",
        "snippet": "Example evidence",
    }]})
    monkeypatch.setattr(research, "search_web", search)
    model = AsyncMock()
    model.complete.return_value = "Supported answer [1]."
    result = await research.research_answer(
        model, "public question", remaining=10
    )
    search.assert_awaited_once_with(
        "http://search:8080", "public question", timeout_seconds=10
    )
    messages = model.complete.await_args.args[0]
    assert len(messages) == 2
    assert "public question" in messages[1].content
    assert "untrusted_search_evidence" in messages[1].content
    model.complete_with_tools.assert_not_awaited()
    assert "https://example.org/" in result
    assert "pages not fetched" in result


@pytest.mark.asyncio
async def test_no_results_skips_model(monkeypatch):
    monkeypatch.setenv("ATLAS_RESEARCH_ENABLED", "1")
    monkeypatch.setenv("ATLAS_SEARCH_BASE_URL", "http://search:8080")
    monkeypatch.setattr(
        research, "search_web", AsyncMock(return_value={"results": []})
    )
    model = AsyncMock()
    result = await research.research_answer(model, "test", remaining=10)
    assert "no usable sources" in result
    model.complete.assert_not_awaited()


# RESEARCH_COORDINATOR_ROUTING_TESTS_V1
from contextlib import ExitStack
from unittest.mock import Mock, patch
from uuid import uuid4

from app.auth import Principal
from app import conversation_coordinator as co


def coordinator_setup(stack, text):
    source = {
        "turn_id": uuid4(),
        "conversation_id": uuid4(),
        "user_message_id": uuid4(),
        "status": "queued",
        "memory_context_sha256": "digest",
    }
    principal = Principal(
        "test-owner", uuid4(),
        frozenset({"conversations:read", "conversations:write"}),
        "user",
    )
    token = uuid4()
    stack.enter_context(patch.object(
        co, "reserve_turn", new_callable=AsyncMock, return_value=source
    ))
    stack.enter_context(patch.object(
        co, "claim_turn", new_callable=AsyncMock,
        return_value=dict(source, worker_token=token)
    ))
    stack.enter_context(patch.object(
        co, "stored_memory_context", return_value=[]
    ))
    stack.enter_context(patch.object(
        co, "_load_turn_context", new_callable=AsyncMock,
        return_value=([{"role": "user", "content": text}], 10)
    ))
    publish = stack.enter_context(patch.object(
        co, "publish_turn", new_callable=AsyncMock,
        return_value={"ok": True}
    ))
    fail = stack.enter_context(patch.object(
        co, "fail_turn", new_callable=AsyncMock
    ))
    return source, principal, publish, fail


@pytest.mark.asyncio
@pytest.mark.parametrize("text,expected_query", [
    ("/search public question", "public question"),
    ("/search\npublic question", "public question"),
])
async def test_explicit_search_routes_before_publication(text, expected_query):
    with ExitStack() as stack:
        source, principal, publish, fail = coordinator_setup(stack, text)
        run = stack.enter_context(patch.object(
            research, "research_answer", new_callable=AsyncMock,
            return_value="Research answer [1]."
        ))
        model = AsyncMock()
        result = await co.execute_turn(
            Mock(), principal, source["conversation_id"],
            source["user_message_id"], model=model,
            write_event=AsyncMock(),
        )
    assert result == {"ok": True}
    run.assert_awaited_once_with(model, expected_query, remaining=10)
    model.complete.assert_not_awaited()
    assert publish.await_args.kwargs["content"] == "Research answer [1]."
    assert publish.await_args.kwargs["tool_calls"] == ()
    fail.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "hello",
    "Please discuss /search without running it.",
    "/searching is not the command",
])
async def test_ordinary_text_never_enters_research(text):
    with ExitStack() as stack:
        source, principal, publish, fail = coordinator_setup(stack, text)
        run = stack.enter_context(patch.object(
            research, "research_answer", new_callable=AsyncMock
        ))
        model = AsyncMock()
        model.complete.return_value = "Ordinary answer."
        await co.execute_turn(
            Mock(), principal, source["conversation_id"],
            source["user_message_id"], model=model,
            write_event=AsyncMock(),
        )
    run.assert_not_awaited()
    model.complete.assert_awaited_once()
    assert publish.await_args.kwargs["content"] == "Ordinary answer."
    fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_research_failure_marks_turn_failed_without_publication():
    with ExitStack() as stack:
        source, principal, publish, fail = coordinator_setup(
            stack, "/search public question"
        )
        stack.enter_context(patch.object(
            research, "research_answer", new_callable=AsyncMock,
            side_effect=SearchError("search unavailable")
        ))
        with pytest.raises(SearchError, match="search unavailable"):
            await co.execute_turn(
                Mock(), principal, source["conversation_id"],
                source["user_message_id"], model=AsyncMock(),
                write_event=AsyncMock(),
            )
    publish.assert_not_awaited()
    fail.assert_awaited_once()
    assert fail.await_args.kwargs["error_code"] == "execution_failed"
