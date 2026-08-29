import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from jarvis_bridge import (  # noqa: E402
    BridgeEngine,
    extract_user_text,
    is_native_intent,
    parse_hermes_output,
    parse_hermes_streams,
    sanitize_for_tts,
    extract_miloco_answer,
    default_run_home,
)


class JarvisBridgeTests(unittest.TestCase):
    def test_extracts_last_user_message_from_migpt_request(self):
        body = {
            "messages": [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "旧问题"},
                {"role": "assistant", "content": "旧回答"},
                {"role": "user", "content": "请看看客厅"},
            ]
        }
        self.assertEqual(extract_user_text(body), "请看看客厅")

    def test_native_intents_are_silent(self):
        texts = [
            "请播放周杰伦", "你暂停一下", "请继续播放", "下一首",
            "请把音量调到三十", "请设置明早七点的闹钟", "你帮我倒计时十分钟",
        ]
        for text in texts:
            with self.subTest(text=text):
                self.assertTrue(is_native_intent(text))

    def test_agent_intents_are_not_bypassed(self):
        texts = [
            "请看看客厅有没有人", "你查一下宠物龟现在怎么样",
            "贾维斯，把客厅灯关掉", "请解释一下量子力学",
        ]
        for text in texts:
            with self.subTest(text=text):
                self.assertFalse(is_native_intent(text))

    def test_parse_first_hermes_turn_extracts_session_and_answer(self):
        sid, answer = parse_hermes_output("session_id: abc_123\n客厅没人。\n")
        self.assertEqual(sid, "abc_123")
        self.assertEqual(answer, "客厅没人。")

    def test_parse_hermes_streams_reads_session_from_stderr(self):
        sid, answer = parse_hermes_streams("客厅没人。\n", "session_id: abc_123\n")
        self.assertEqual(sid, "abc_123")
        self.assertEqual(answer, "客厅没人。")

    def test_sanitize_removes_markdown_thinking_and_tool_noise(self):
        text = "# 结果\n<think>内部分析</think>\n**客厅没人。**\n工具调用完成"
        self.assertEqual(sanitize_for_tts(text), "结果 客厅没人。")

    def test_native_intent_returns_empty_openai_answer(self):
        with TemporaryDirectory() as d:
            called = []
            engine = BridgeEngine(
                state_path=Path(d) / "state.json",
                run_hermes=lambda text, sid: called.append((text, sid)),
                run_fallback=lambda body: "不该调用",
            )
            result = engine.complete({"model": "x", "messages": [{"role": "user", "content": "请播放音乐"}]})
            self.assertEqual(result["choices"][0]["message"]["content"], "")
            self.assertEqual(called, [])

    def test_agent_turn_persists_and_resumes_session(self):
        with TemporaryDirectory() as d:
            calls = []

            def fake_hermes(text, sid):
                calls.append((text, sid))
                if sid is None:
                    return "session_1", "第一轮回答"
                return sid, "第二轮回答"

            state = Path(d) / "state.json"
            engine = BridgeEngine(state_path=state, run_hermes=fake_hermes, run_fallback=lambda body: "fallback")
            first = engine.complete({"messages": [{"role": "user", "content": "请问客厅有人吗"}]})
            second = engine.complete({"messages": [{"role": "user", "content": "那宠物龟呢"}]})

            self.assertEqual(first["choices"][0]["message"]["content"], "第一轮回答")
            self.assertEqual(second["choices"][0]["message"]["content"], "第二轮回答")
            self.assertEqual(calls, [("请问客厅有人吗", None), ("那宠物龟呢", "session_1")])
            self.assertEqual(json.loads(state.read_text())["session_id"], "session_1")


    def test_hermes_failure_falls_back_to_ollama(self):
        with TemporaryDirectory() as d:
            def fail(_text, _sid):
                raise TimeoutError("Hermes timeout")

            engine = BridgeEngine(
                state_path=Path(d) / "state.json", run_hermes=fail,
                run_fallback=lambda body: "本地回退回答",
            )
            result = engine.complete({"messages": [{"role": "user", "content": "请讲个笑话"}]})
            self.assertEqual(result["choices"][0]["message"]["content"], "本地回退回答")
            self.assertEqual(result["jarvis"]["route"], "fallback")

    def test_realtime_home_query_never_falls_back_to_generative_model(self):
        with TemporaryDirectory() as d:
            fallback_calls = []

            def fail(_text, _sid):
                raise TimeoutError("Hermes timeout")

            engine = BridgeEngine(
                state_path=Path(d) / "state.json",
                run_hermes=fail,
                run_fallback=lambda body: fallback_calls.append(body) or "虚构有一台摄像头",
            )
            result = engine.complete({"messages": [{"role": "user", "content": "请查看现在有几台在线摄像头"}]})
            self.assertEqual(fallback_calls, [])
            self.assertEqual(result["jarvis"]["route"], "home-unavailable")
            self.assertIn("暂时无法确认", result["choices"][0]["message"]["content"])

    def test_extract_miloco_answer_supports_nested_data(self):
        self.assertEqual(
            extract_miloco_answer({"code": 0, "data": {"answer": "两台摄像头都在线。"}}),
            "两台摄像头都在线。",
        )

    def test_natural_pet_activity_reads_recent_logs_without_live_query(self):
        devices = {"data": [{"name": "示例摄像头A", "did": "hamster", "online": True}]}
        logs = '2026-07-27T10:40:00+08:00: {"示例房间": "宠物仓鼠在黄色小碗旁边。"}\n'
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            output = json.dumps(devices, ensure_ascii=False) if argv[-2:] == ["perceive", "devices"] else logs
            return type("Result", (), {"returncode": 0, "stdout": output})()

        with patch("jarvis_bridge.subprocess.run", side_effect=fake_run):
            answer = default_run_home("宠物仓鼠在干嘛")

        self.assertIn("10:40", answer)
        self.assertIn("黄色小碗旁边", answer)
        self.assertFalse(any("query" in argv for argv in calls))

    def test_natural_pet_activity_without_recent_log_does_not_live_query(self):
        devices = {"data": [{"name": "示例摄像头A", "did": "hamster", "online": True}]}
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            output = json.dumps(devices, ensure_ascii=False) if argv[-2:] == ["perceive", "devices"] else ""
            return type("Result", (), {"returncode": 0, "stdout": output})()

        with patch("jarvis_bridge.subprocess.run", side_effect=fake_run):
            answer = default_run_home("看看宠物仓鼠在干嘛")

        self.assertIn("最近的感知日志没有记录到宠物仓鼠的明确活动", answer)
        self.assertFalse(any("query" in argv for argv in calls))

    def test_explicit_realtime_camera_request_still_uses_live_query(self):
        devices = {"data": [{"name": "示例摄像头A", "did": "hamster", "online": True}]}
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[-2:] == ["perceive", "devices"]:
                output = json.dumps(devices, ensure_ascii=False)
            else:
                output = json.dumps({"data": {"answer": "宠物仓鼠在黄色小碗旁边。"}}, ensure_ascii=False)
            return type("Result", (), {"returncode": 0, "stdout": output})()

        with patch("jarvis_bridge.subprocess.run", side_effect=fake_run):
            answer = default_run_home("现在实时看一下宠物仓鼠")

        self.assertIn("黄色小碗旁边", answer)
        self.assertTrue(any("query" in argv for argv in calls))

    def test_local_only_chat_bypasses_hermes_and_uses_local_model(self):
        with TemporaryDirectory() as d:
            hermes_calls = []
            engine = BridgeEngine(
                state_path=Path(d) / "state.json",
                run_hermes=lambda text, sid: hermes_calls.append((text, sid)),
                run_fallback=lambda body: "本地模型回答",
                run_home=lambda text: "不该调用",
                local_only=True,
            )
            result = engine.complete({"messages": [{"role": "user", "content": "讲个笑话"}]})
            self.assertEqual(hermes_calls, [])
            self.assertEqual(result["jarvis"]["route"], "local-model")
            self.assertEqual(result["choices"][0]["message"]["content"], "本地模型回答")

    def test_local_only_camera_query_bypasses_model_and_calls_miloco(self):
        with TemporaryDirectory() as d:
            fallback_calls = []
            home_calls = []
            engine = BridgeEngine(
                state_path=Path(d) / "state.json",
                run_hermes=lambda *_: (_ for _ in ()).throw(AssertionError("Hermes must not run")),
                run_fallback=lambda body: fallback_calls.append(body) or "模型猜测",
                run_home=lambda text: home_calls.append(text) or "宠物仓鼠正在跑轮。",
                local_only=True,
            )
            result = engine.complete({"messages": [{"role": "user", "content": "现在示例摄像头A在干嘛"}]})
            self.assertEqual(fallback_calls, [])
            self.assertEqual(home_calls, ["现在示例摄像头A在干嘛"])
            self.assertEqual(result["jarvis"]["route"], "local-miloco")
            self.assertIn("宠物仓鼠", result["choices"][0]["message"]["content"])

    def test_empty_user_message_is_rejected(self):
        with TemporaryDirectory() as d:
            engine = BridgeEngine(
                state_path=Path(d) / "state.json", run_hermes=lambda *_: None,
                run_fallback=lambda *_: "",
            )
            with self.assertRaisesRegex(ValueError, "user message"):
                engine.complete({"messages": [{"role": "system", "content": "x"}]})

    def test_tts_answer_is_bounded(self):
        with TemporaryDirectory() as d:
            engine = BridgeEngine(
                state_path=Path(d) / "state.json",
                run_hermes=lambda text, sid: ("s", "很长" * 200),
                run_fallback=lambda body: "fallback",
            )
            result = engine.complete({"messages": [{"role": "user", "content": "请回答"}]})
            content = result["choices"][0]["message"]["content"]
            self.assertLessEqual(len(content), 240)
            self.assertTrue(content.endswith("。"))


if __name__ == "__main__":
    unittest.main()
