import json
import unittest

from local_chat import GENERAL_CHAT_PROMPT, build_payload, normalize_sse_line


class LocalChatBackendTests(unittest.TestCase):
    def test_payload_uses_voice_prompt_and_short_output_limit(self):
        body = {
            "model": "jarvis",
            "messages": [{"role": "user", "content": "给我讲个笑话"}],
            "stream": True,
            "max_tokens": 999,
        }
        payload = build_payload(body)
        self.assertEqual(payload["model"], "qwen35-4b-16k:latest")
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["options"]["num_predict"], 160)
        self.assertEqual(payload["options"]["num_ctx"], 16384)
        self.assertEqual(payload["max_tokens"], 160)
        self.assertEqual(payload["messages"][0], {"role": "system", "content": GENERAL_CHAT_PROMPT})
        self.assertNotIn("家庭智能控制", GENERAL_CHAT_PROMPT)

    def test_airi_surface_uses_the_visual_interface_identity_without_changing_session_history(self):
        payload = build_payload({
            "surface": "airi",
            "messages": [{"role": "user", "content": "你在哪里"}],
        })
        prompt = payload["messages"][0]["content"]
        self.assertIn("数字形象界面", prompt)
        self.assertNotIn("小爱音箱", prompt)

    def test_voice_prompt_requires_a_short_complete_lead_sentence(self):
        payload = build_payload({"messages": [{"role": "user", "content": "解释量子纠缠"}]})
        prompt = payload["messages"][0]["content"]
        self.assertIn("第一句不超过20个汉字", prompt)
        self.assertIn("完整中文句号", prompt)

    def test_voice_prompt_forbids_claiming_unobserved_home_or_pet_state(self):
        prompt = build_payload({"messages": [{"role": "user", "content": "宠物仓鼠在干嘛"}]})["messages"][0]["content"]
        self.assertIn("不得声称正在查看", prompt)
        self.assertIn("家庭、宠物或设备的当前状态", prompt)
        self.assertIn("需要查询实时记录", prompt)

    def test_payload_keeps_only_recent_conversation(self):
        messages = [{"role": "user", "content": str(i)} for i in range(20)]
        payload = build_payload({"messages": messages, "stream": False})
        self.assertEqual(len(payload["messages"]), 11)
        self.assertEqual(payload["messages"][1]["content"], "10")
        self.assertEqual(payload["messages"][-1]["content"], "19")

    def test_sse_normalizer_accepts_only_openai_data_events(self):
        chunk = {"choices": [{"delta": {"content": "你好"}}]}
        line = "data: " + json.dumps(chunk, ensure_ascii=False)
        normalized = normalize_sse_line(line)
        self.assertEqual(json.loads(normalized[6:].strip()), chunk)
        self.assertEqual(normalize_sse_line("data: [DONE]"), "data: [DONE]\n\n")
        self.assertIsNone(normalize_sse_line(""))
        self.assertIsNone(normalize_sse_line("event: ping"))
        self.assertIsNone(normalize_sse_line("data: not-json"))


if __name__ == "__main__":
    unittest.main()
