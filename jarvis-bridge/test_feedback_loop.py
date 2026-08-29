import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from feedback_loop import FeedbackLedger, RouteReview, build_review_prompt, parse_review


class FeedbackLoopTests(unittest.TestCase):
    def test_speaker_channel_reuses_actual_session_within_thirty_minutes(self):
        with TemporaryDirectory() as directory:
            now = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
            ledger = FeedbackLedger(Path(directory), now=lambda: now)
            first = ledger.resolve_session("speaker-speaker-1")
            second = ledger.resolve_session("speaker-speaker-1")
            self.assertEqual(first, second)
            self.assertNotEqual(first, "speaker-speaker-1")

    def test_speaker_channel_rotates_after_idle_timeout_or_local_day_change(self):
        with TemporaryDirectory() as directory:
            clock = [datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)]
            ledger = FeedbackLedger(Path(directory), now=lambda: clock[0])
            first = ledger.resolve_session("speaker-speaker-1")
            clock[0] += timedelta(minutes=31)
            after_idle = ledger.resolve_session("speaker-speaker-1")
            self.assertNotEqual(first, after_idle)
            clock[0] = datetime(2026, 7, 31, 0, 1, tzinfo=timezone.utc)
            after_day = ledger.resolve_session("speaker-speaker-1")
            self.assertNotEqual(after_idle, after_day)

    def test_explicit_web_session_remains_stable(self):
        with TemporaryDirectory() as directory:
            ledger = FeedbackLedger(Path(directory))
            self.assertEqual(ledger.resolve_session("web-abc123"), "web-abc123")

    def test_turn_persists_sanitized_hierarchical_arbitration_metadata(self):
        with TemporaryDirectory() as directory:
            ledger = FeedbackLedger(Path(directory))
            turn = ledger.save_turn({
                "request": "家里有几台摄像头", "route": "home", "intent": "query",
                "executor": "bridge_recipe", "producer": "miloco",
                "capability": "camera_inventory", "capability_version": 2,
                "answer": "3台", "success": True,
                "arbitration_id": "arb-abc123",
                "level1": {
                    "decision": "handoff", "quick_tool_id": "none",
                    "handoff": "lookup", "confidence": 0.98,
                    "prompt": "must not persist",
                },
                "level2": {
                    "decision": "quick_tool", "quick_tool_id": "camera_inventory",
                    "relation": "consistent", "confidence": 0.97,
                    "arguments": {"secret": "must not persist"},
                },
                "decision_tier": "4b", "execution_class": "quick_tool",
                "tool_class": "deterministic_query", "selector_tier": "4b_eligible",
                "risk_class": "read_only",
            })
            self.assertEqual(turn["arbitration_id"], "arb-abc123")
            self.assertEqual(turn["level1"], {
                "decision": "handoff", "quick_tool_id": "none",
                "handoff": "lookup", "confidence": 0.98,
            })
            self.assertEqual(turn["level2"], {
                "decision": "quick_tool", "quick_tool_id": "camera_inventory",
                "relation": "consistent", "confidence": 0.97,
            })
            self.assertEqual(turn["decision_tier"], "4b")
            self.assertEqual(turn["execution_class"], "quick_tool")
            self.assertEqual(turn["risk_class"], "read_only")
            self.assertNotIn("prompt", json.dumps(turn))
            self.assertNotIn("secret", json.dumps(turn))

    def test_turn_rejects_invalid_arbitration_taxonomy(self):
        with TemporaryDirectory() as directory:
            ledger = FeedbackLedger(Path(directory))
            base = {
                "request": "测试", "route": "home", "intent": "query",
                "executor": "openclaw", "answer": "", "success": False,
            }
            for field, value in (
                ("decision_tier", "admin"),
                ("execution_class", "shell"),
                ("tool_class", "../../secret"),
                ("selector_tier", "auto_pay"),
                ("risk_class", "unbounded_shell"),
            ):
                invalid = dict(base)
                invalid[field] = value
                with self.assertRaises(ValueError):
                    ledger.save_turn(invalid)

    def test_turn_persists_only_sanitized_tool_trace(self):
        with TemporaryDirectory() as directory:
            ledger = FeedbackLedger(Path(directory))
            turn = ledger.save_turn({
                "request": "打开灯", "route": "home", "intent": "action",
                "executor": "openclaw", "producer": "openclaw",
                "answer": "已打开", "success": True,
                "tool_trace": [{
                    "provider": "openclaw", "tool": "miloco-cli",
                    "status": "completed", "arguments": {"secret": "no"},
                }],
            })
            self.assertEqual(turn["tool_trace"], [{
                "provider": "openclaw", "tool": "miloco-cli", "status": "completed",
            }])
            self.assertNotIn("secret", json.dumps(turn))

    def test_turn_persists_only_bounded_safe_timing_metadata(self):
        with TemporaryDirectory() as directory:
            ledger = FeedbackLedger(Path(directory))
            turn = ledger.save_turn({
                "request": "看看宠物仓鼠", "route": "arbitrated", "intent": "openclaw",
                "executor": "openclaw", "producer": "openclaw",
                "answer": "没有看到宠物仓鼠。", "success": True,
                "level1": {
                    "decision": "handoff", "quick_tool_id": "none",
                    "handoff": "lookup", "confidence": 0.99, "elapsed_ms": 1855,
                },
                "level2": {
                    "decision": "openclaw", "quick_tool_id": "none",
                    "relation": "escalated", "confidence": 0.98, "elapsed_ms": 2100,
                },
                "execution_elapsed_ms": 8400,
                "tool_trace": [{
                    "provider": "openclaw", "tool": "exec", "status": "completed",
                    "elapsed_ms": 320, "arguments": {"secret": "no"},
                }],
            })
            self.assertEqual(turn["level1"]["elapsed_ms"], 1855)
            self.assertEqual(turn["level2"]["elapsed_ms"], 2100)
            self.assertEqual(turn["execution_elapsed_ms"], 8400)
            self.assertEqual(turn["tool_trace"], [{
                "provider": "openclaw", "tool": "exec", "status": "completed",
                "elapsed_ms": 320,
            }])
            self.assertNotIn("secret", json.dumps(turn))

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ledger = FeedbackLedger(self.root)


    def test_last_turn_and_feedback_files_are_private(self):
        turn = self.ledger.save_turn({
            "request": "宠物仓鼠在干嘛", "route": "camera",
            "intent": "camera_recent", "executor": "miloco",
            "answer": "没有看到宠物仓鼠。", "success": True,
        })
        self.assertEqual(self.ledger.last_turn()["turn_id"], turn["turn_id"])
        self.ledger.append_feedback(turn, RouteReview(
            route="local_chat", intent="chat", executor="local_9b",
            confidence=0.96, safe_to_retry=True, reason="用户实际在闲聊",
        ), "不对")
        self.assertEqual((self.root / "last-turn.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "feedback.jsonl").stat().st_mode & 0o777, 0o600)
        row = json.loads((self.root / "feedback.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["original_turn_id"], turn["turn_id"])
        self.assertEqual(row["review_route"], "local_chat")

    def test_review_parser_is_strict(self):
        review = parse_review(json.dumps({
            "route": "camera", "intent": "camera_recent", "executor": "miloco",
            "capability": "none", "confidence": 0.97,
            "safe_to_retry": True, "reason": "应读取近期日志",
        }))
        self.assertEqual(review.route, "camera")
        web_review = parse_review(json.dumps({
            "route": "web_query", "intent": "query", "executor": "openclaw",
            "confidence": 0.99, "safe_to_retry": True, "reason": "需要外部实时信息",
        }))
        self.assertEqual((web_review.route, web_review.executor), ("web_query", "openclaw"))
        capability_review = parse_review(json.dumps({
            "route": "home", "intent": "query", "executor": "miloco",
            "capability": "camera_inventory", "confidence": 0.99,
            "safe_to_retry": True, "reason": "摄像头目录应由能力包执行",
        }))
        self.assertEqual(capability_review.capability, "camera_inventory")
        with self.assertRaises(ValueError):
            parse_review('{"route":"shell","intent":"exec","executor":"exec","confidence":1,"safe_to_retry":true,"reason":"x"}')
        with self.assertRaises(ValueError):
            parse_review('{"route":"home"}')

    def test_openclaw_review_becomes_bounded_router_prompt_example(self):
        turn = self.ledger.save_turn({
            "request": "以后每晚看看饮水器", "route": "camera",
            "intent": "camera_recent", "executor": "miloco",
            "answer": "没有记录。", "success": True,
        })
        review = RouteReview(
            route="task", intent="create", executor="openclaw",
            confidence=0.98, safe_to_retry=False, reason="这是持续任务",
        )
        self.ledger.append_feedback(turn, review, "不对")
        self.assertEqual(self.ledger.prompt_examples(), [])
        examples = self.ledger.candidate_examples()
        self.assertEqual(examples[-1], {
            "request": "以后每晚看看饮水器",
            "route": "task",
            "intent": "create",
            "executor": "openclaw",
        })
        self.assertLessEqual(len(examples), 20)
        self.assertEqual((self.root / "router-candidate-examples.json").stat().st_mode & 0o777, 0o600)

    def test_mutating_routes_are_never_automatically_retried(self):
        for intent in ("create", "update", "delete", "action"):
            review = RouteReview(
                route="task" if intent != "action" else "home",
                intent=intent,
                executor="openclaw",
                confidence=0.99,
                safe_to_retry=True,
                reason="修正路线",
            )
            self.assertFalse(review.can_auto_retry)
        query = RouteReview(
            route="camera", intent="camera_recent", executor="miloco",
            confidence=0.99, safe_to_retry=True, reason="修正路线",
        )
        self.assertTrue(query.can_auto_retry)

    def test_capability_review_compares_actual_producer_not_planned_recipe(self):
        turn = self.ledger.save_turn({
            "request": "家里有几台摄像头", "route": "home", "intent": "query",
            "executor": "bridge_recipe", "producer": "miloco",
            "capability": "camera_inventory", "capability_version": 1,
            "answer": "3台", "success": True,
        })
        review = RouteReview("home", "query", "miloco", 0.99, True, "路线正确", "camera_inventory")
        self.ledger.append_feedback(turn, review, "复审")
        self.assertEqual(self.ledger.candidate_examples(), [])
        prompt = build_review_prompt(turn, "复审")
        self.assertIn('"planned_executor": "bridge_recipe"', prompt)
        self.assertIn('"executor": "miloco"', prompt)

    def test_capability_turn_preserves_actual_execution_metadata(self):
        turn = self.ledger.save_turn({
            "session_id": "speaker-1", "request": "家里有几台摄像头",
            "route": "home", "intent": "query", "executor": "bridge_recipe",
            "producer": "miloco", "capability": "camera_inventory",
            "capability_version": 1, "answer": "家里一共3台摄像头。", "success": True,
        })
        self.assertEqual(turn["executor"], "bridge_recipe")
        self.assertEqual(turn["producer"], "miloco")
        self.assertEqual(turn["capability"], "camera_inventory")
        self.assertEqual(turn["capability_version"], 1)

    def test_matching_review_is_logged_without_creating_prompt_candidate(self):
        turn = self.ledger.save_turn({
            "request": "讲个笑话", "route": "local_chat", "intent": "chat",
            "executor": "local_9b", "answer": "一个笑话。", "success": True,
        })
        review = RouteReview(
            route="local_chat", intent="chat", executor="local_9b",
            confidence=0.95, safe_to_retry=True, reason="路由正确，只是回答质量问题",
        )
        self.ledger.append_feedback(turn, review, "webui_explicit_review")
        self.assertEqual(self.ledger.candidate_examples(), [])
        self.assertTrue(self.ledger.feedback_path.exists())

    def test_sessions_are_listed_and_selected_conversation_returns_only_its_turns(self):
        first = self.ledger.save_turn({
            "session_id": "session-a", "request": "第一问", "route": "local_chat", "intent": "chat",
            "executor": "local_9b", "answer": "第一答", "success": True,
        })
        self.ledger.save_turn({
            "session_id": "session-b", "request": "另一问", "route": "home", "intent": "query",
            "executor": "openclaw", "answer": "另一答", "success": True,
        })
        second = self.ledger.save_turn({
            "session_id": "session-a", "request": "第二问", "route": "camera", "intent": "camera_recent",
            "executor": "miloco", "answer": "第二答", "success": True,
        })
        sessions = self.ledger.list_sessions()
        by_id = {item["session_id"]: item for item in sessions}
        self.assertEqual(by_id["session-a"]["title"], "第一问")
        self.assertEqual(by_id["session-a"]["turn_count"], 2)
        self.assertEqual([x["turn_id"] for x in self.ledger.session_turns("session-a")], [first["turn_id"], second["turn_id"]])

    def test_legacy_turns_are_never_guessed_into_one_conversation(self):
        rows = [
            {"turn_id": "old-a", "time": "2026-07-26T10:00:00+00:00", "request": "旧问题一", "route": "local_chat", "intent": "chat", "executor": "local_9b", "answer": "旧回答一", "success": True},
            {"turn_id": "old-b", "time": "2026-07-26T10:01:00+00:00", "request": "旧问题二", "route": "local_chat", "intent": "chat", "executor": "local_9b", "answer": "旧回答二", "success": True},
        ]
        self.ledger.turns_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        sessions = {item["session_id"] for item in self.ledger.list_sessions()}
        self.assertEqual(sessions, {"legacy-old-a", "legacy-old-b"})

    def test_list_turns_returns_recent_stable_history_oldest_first(self):
        saved = []
        for index in range(3):
            saved.append(self.ledger.save_turn({
                "request": f"第{index + 1}轮", "route": "local_chat", "intent": "chat",
                "executor": "local_9b", "answer": f"答{index + 1}", "success": True,
            }))
        with self.ledger.turns_path.open("a", encoding="utf-8") as handle:
            handle.write("broken-json\n")
        history = self.ledger.list_turns(limit=2)
        self.assertEqual([item["turn_id"] for item in history], [saved[1]["turn_id"], saved[2]["turn_id"]])
        self.assertEqual([item["request"] for item in history], ["第2轮", "第3轮"])

    def test_turn_preserves_structured_escalation_reason(self):
        turn = self.ledger.save_turn({
            "request": "读取目录", "route": "home", "intent": "query",
            "executor": "bridge_recipe", "producer": "openclaw",
            "capability": "camera_inventory", "capability_version": 1,
            "escalation_reason": "data_source_unavailable",
            "answer": "已核验。", "success": True,
        })
        self.assertEqual(turn["escalation_reason"], "data_source_unavailable")


if __name__ == "__main__":
    unittest.main()
