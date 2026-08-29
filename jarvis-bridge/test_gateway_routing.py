import unittest
from unittest.mock import AsyncMock, patch

import api_server
from router import RouteDecision
from semantic_router import SemanticRouter


async def collect(source):
    return "".join([chunk async for chunk in source])


class FakeLocalChat:
    def __init__(self, events=None, error=None):
        self.events = events or []
        self.error = error
        self.calls = []

    async def stream(self, body):
        self.calls.append(body)
        if self.error:
            raise self.error
        for event in self.events:
            yield event


class GatewayRoutingTests(unittest.IsolatedAsyncioTestCase):
    def test_production_has_no_semantic_rule_router(self):
        self.assertFalse(hasattr(api_server, "router"))
        self.assertIsInstance(api_server.intent_router, SemanticRouter)
        self.assertEqual(type(api_server.intent_router.fallback).__name__, "Router")

    def test_legacy_bridge_adapter_is_removed(self):
        self.assertFalse(hasattr(api_server, "_legacy_local_stream"))

    def chat_decision(self):
        return RouteDecision(api_server.Route.LOCAL_CHAT, "chat", "semantic_chat")

    async def test_general_chat_uses_direct_local_backend(self):
        backend = FakeLocalChat([
            'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n',
            "data: [DONE]\n\n",
        ])
        body = {"stream": True, "messages": [{"role": "user", "content": "你好"}]}
        with patch.object(api_server.intent_router, "decide", AsyncMock(return_value=self.chat_decision())), patch.object(api_server, "local_chat", backend):
            output = await collect(api_server._stream_chat(body))
        self.assertIn("你好", output)
        self.assertEqual(backend.calls[0]["messages"][-1], {"role": "user", "content": "你好"})
        self.assertTrue(backend.calls[0]["stream"])

    async def test_direct_local_failure_does_not_revive_legacy_bridge(self):
        backend = FakeLocalChat(error=ConnectionError("ollama unavailable"))
        body = {"stream": True, "messages": [{"role": "user", "content": "你好"}]}
        with patch.object(api_server.intent_router, "decide", AsyncMock(return_value=self.chat_decision())), patch.object(api_server, "local_chat", backend):
            output = await collect(api_server._stream_chat(body))
        self.assertIn("本地聊天暂时不可用", output)
        self.assertIn("data: [DONE]", output)
        self.assertNotIn("18085", output)

    async def test_device_knowledge_question_stays_local_when_model_says_chat(self):
        backend = FakeLocalChat(["data: [DONE]\n\n"])
        body = {"stream": True, "messages": [{"role": "user", "content": "解释一下空调为什么能制冷"}]}
        with patch.object(api_server.intent_router, "decide", AsyncMock(return_value=self.chat_decision())), patch.object(api_server, "local_chat", backend), patch.object(api_server, "_openclaw_ws_stream") as cloud:
            await collect(api_server._stream_chat(body))
        self.assertEqual(len(backend.calls), 1)
        cloud.assert_not_called()


if __name__ == "__main__":
    unittest.main()
