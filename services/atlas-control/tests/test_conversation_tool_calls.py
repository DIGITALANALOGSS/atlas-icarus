"""Offline tests: no live server, no tool execution."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from app.conversation_adapters import (
    ConversationTurn, ConversationModelError, LlamaCppConversationModel,
)
from app.conversation_tool_calls import (
    ToolCapableConversationModel, parse_reply,
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "atlas_probe",
        "parameters": {"type": "object"},
    },
}]

def reply():
    return {"choices": [{
        "finish_reason": "tool_calls",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "probe-1",
                "type": "function",
                "function": {
                    "name": "atlas_probe",
                    "arguments": '{"marker":"QUALIFY_ONLY"}',
                },
            }],
        },
    }]}

class ParsingTests(unittest.TestCase):
    def test_valid_call(self):
        result = parse_reply(reply(), {"atlas_probe"})
        self.assertEqual(result.tool_calls[0].id, "probe-1")
        self.assertEqual(
            result.tool_calls[0].arguments(), {"marker": "QUALIFY_ONLY"}
        )

    def test_text_reply(self):
        data = {"choices": [{
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": "hello"},
        }]}
        self.assertEqual(parse_reply(data, {"atlas_probe"}).content, "hello")

    def test_invalid_arguments(self):
        for raw in (
            "not-json", "[]", '{"x":NaN}', '{"x":1,"x":2}',
        ):
            with self.subTest(raw=raw):
                data = reply()
                data["choices"][0]["message"]["tool_calls"][0][
                    "function"
                ]["arguments"] = raw
                with self.assertRaises(ConversationModelError):
                    parse_reply(data, {"atlas_probe"})

    def test_invalid_call_metadata(self):
        for kind in ("name", "duplicate", "finish", "id", "role"):
            with self.subTest(kind=kind):
                data = reply()
                choice = data["choices"][0]
                call = choice["message"]["tool_calls"][0]
                if kind == "name":
                    call["function"]["name"] = "not_allowed"
                elif kind == "duplicate":
                    choice["message"]["tool_calls"].append(copy.deepcopy(call))
                elif kind == "finish":
                    choice["finish_reason"] = "length"
                elif kind == "id":
                    call["id"] = ""
                else:
                    choice["message"]["role"] = "user"
                with self.assertRaises(ConversationModelError):
                    parse_reply(data, {"atlas_probe"})

    def test_text_methods_unchanged(self):
        self.assertIs(
            ToolCapableConversationModel.complete,
            LlamaCppConversationModel.complete,
        )
        self.assertIs(
            ToolCapableConversationModel.stream,
            LlamaCppConversationModel.stream,
        )

class TransportTests(unittest.IsolatedAsyncioTestCase):
    def model(self, timeout=1):
        return ToolCapableConversationModel(
            base_url="http://test.invalid/v1",
            model="probe", api_key="private-test-key",
            timeout_seconds=timeout,
        )

    def factory(self, handler):
        original = httpx.AsyncClient
        def create(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return original(
                transport=httpx.MockTransport(handler), **kwargs
            )
        return create

    async def test_request_and_response(self):
        def handler(request):
            body = json.loads(request.content)
            self.assertEqual(body["tools"], TOOLS)
            self.assertEqual(body["tool_choice"], "auto")
            self.assertFalse(body["stream"])
            self.assertEqual(
                body["messages"], [{"role": "user", "content": "probe"}]
            )
            self.assertEqual(
                request.headers["authorization"], "Bearer private-test-key"
            )
            return httpx.Response(200, json=reply())
        with patch("httpx.AsyncClient", self.factory(handler)):
            result = await self.model().complete_with_tools(
                [ConversationTurn("user", "probe")], tools=TOOLS
            )
        self.assertEqual(result.finish_reason, "tool_calls")

    async def test_sanitized_errors_without_retry(self):
        for mode in ("status", "json", "network"):
            count = 0
            def handler(request):
                nonlocal count
                count += 1
                if mode == "network":
                    raise httpx.ConnectError(
                        "PRIVATE_DETAIL", request=request
                    )
                return httpx.Response(
                    503 if mode == "status" else 200,
                    text="PRIVATE_DETAIL",
                )
            with self.subTest(mode=mode):
                with patch("httpx.AsyncClient", self.factory(handler)):
                    with self.assertRaises(ConversationModelError) as caught:
                        await self.model().complete_with_tools([], tools=TOOLS)
                self.assertNotIn("PRIVATE_DETAIL", str(caught.exception))
                self.assertEqual(count, 1)

    async def test_bad_definitions_make_no_request(self):
        for tools in ([], {}, [{}], TOOLS + TOOLS):
            with self.subTest(tools=tools):
                with patch("httpx.AsyncClient") as factory:
                    with self.assertRaises(ConversationModelError):
                        await self.model().complete_with_tools([], tools=tools)
                    factory.assert_not_called()

    async def test_timeout(self):
        async def handler(request):
            await asyncio.sleep(10)
        with patch("httpx.AsyncClient", self.factory(handler)):
            with self.assertRaisesRegex(ConversationModelError, "timed out"):
                await self.model(.02).complete_with_tools([], tools=TOOLS)

    async def test_cancellation(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        async def handler(request):
            entered.set()
            try:
                await asyncio.sleep(10)
            finally:
                closed.set()
        with patch("httpx.AsyncClient", self.factory(handler)):
            task = asyncio.create_task(
                self.model().complete_with_tools([], tools=TOOLS)
            )
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(closed.is_set())
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

if __name__ == "__main__":
    unittest.main(verbosity=2)
