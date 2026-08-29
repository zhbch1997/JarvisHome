import unittest
from unittest.mock import ANY, AsyncMock, patch

import api_server
from router import RouteDecision


class NonStreamRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_chat_uses_direct_ollama_backend(self):
        expected = {"choices": [{"message": {"role": "assistant", "content": "你好"}}]}
        body = {"messages": [{"role": "user", "content": "你好"}], "stream": False}
        decision = RouteDecision(api_server.Route.LOCAL_CHAT, "chat", "semantic_chat")
        with patch.object(api_server.intent_router, "decide", AsyncMock(return_value=decision)), patch.object(api_server.local_chat, "complete", AsyncMock(return_value=expected)) as call:
            result = await api_server._complete_chat(body)
        self.assertEqual(result, expected)
        call.assert_awaited_once_with(body)

    async def test_native_command_returns_empty_answer(self):
        body = {"model": "jarvis", "messages": [{"role": "user", "content": "播放周杰伦"}]}
        result = await api_server._complete_chat(body)
        self.assertEqual(result["choices"][0]["message"]["content"], "")
        self.assertEqual(result["jarvis"]["route"], "native")

    async def test_camera_query_uses_miloco_backend(self):
        body = {"model": "jarvis", "messages": [{"role": "user", "content": "家里有几台摄像头"}]}
        decision = RouteDecision(api_server.Route.CAMERA, "camera_recent", "semantic_camera_recent", "read_only")
        with patch.object(api_server.intent_router, "decide", AsyncMock(return_value=decision)), patch.object(api_server, "run_miloco_query", return_value="家里有2台摄像头。") as call:
            result = await api_server._complete_chat(body)
        self.assertIn("2台", result["choices"][0]["message"]["content"])
        self.assertEqual(result["jarvis"]["route"], "camera")
        call.assert_called_once_with("家里有几台摄像头")

    async def test_home_query_uses_openclaw_backend(self):
        body = {"model": "jarvis", "messages": [{"role": "user", "content": "查询饲养灯状态"}]}
        decision = RouteDecision(api_server.Route.HOME, "query", "semantic_home", "read_only")
        with patch.object(api_server.intent_router, "decide", AsyncMock(return_value=decision)), patch.object(api_server, "run_openclaw", AsyncMock(return_value="饲养灯开着。")) as call:
            result = await api_server._complete_chat(body)
        self.assertEqual(result["choices"][0]["message"]["content"], "饲养灯开着。")
        self.assertEqual(result["jarvis"]["route"], "home")
        call.assert_awaited_once_with(body, api_server.Route.HOME, tool_trace=ANY)

    def test_production_module_no_longer_instantiates_bridge_engine(self):
        self.assertFalse(hasattr(api_server, "engine"))


if __name__ == "__main__":
    unittest.main()
