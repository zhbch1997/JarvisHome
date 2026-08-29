import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from capability_defaults import camera_inventory_bundle
from capability_registry import CapabilityRegistry
from router import Route, Router
from semantic_router import OllamaIntentClassifier, SemanticRouter, build_router_prompt


class FakeClassifier:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def classify(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return self.result


class RouterPromptTests(unittest.TestCase):
    def test_classifier_defaults_to_dedicated_router_service_and_model(self):
        classifier = OllamaIntentClassifier()
        self.assertEqual(classifier.url, "http://127.0.0.1:11435/api/generate")
        self.assertEqual(classifier.model, "qwen35-router:0.8b")

    def test_classifier_reads_current_active_capabilities_for_each_prompt(self):
        with TemporaryDirectory() as directory:
            registry = CapabilityRegistry(Path(directory) / "capabilities")
            bundle = camera_inventory_bundle()
            bundle["routing"]["examples"] = ["第一版表达"]
            registry.install_initial(bundle)
            classifier = OllamaIntentClassifier(capability_registry=registry)
            first = classifier.system_prompt()
            candidate = camera_inventory_bundle()
            candidate["version"] = 2
            candidate["status"] = "proposed"
            candidate["routing"]["examples"] = ["运行时更新表达"]
            registry.propose(candidate, evidence_ids=["runtime-update"])
            registry._promote_validated("camera_inventory", expected_version=1)
            second = classifier.system_prompt()

        self.assertIn("第一版表达", first)
        self.assertNotIn("第一版表达", second)
        self.assertIn("运行时更新表达", second)

    def test_prompt_classifies_camera_inventory_as_grounded_home_query(self):
        prompt = build_router_prompt(Path("/definitely/missing/router-examples.json"))
        self.assertIn('“家里有几台摄像头”=>home/query/camera_inventory', prompt)

    def test_prompt_uses_active_capability_examples_but_never_candidates(self):
        with TemporaryDirectory() as directory:
            registry = CapabilityRegistry(Path(directory) / "capabilities")
            active = camera_inventory_bundle()
            active["routing"]["examples"] = ["列出家里的监控设备"]
            registry.install_initial(active)
            candidate = camera_inventory_bundle()
            candidate["version"] = 2
            candidate["status"] = "proposed"
            candidate["routing"]["examples"] = ["这个候选还不能生效"]
            registry.propose(candidate, ["turn-1"])

            prompt = build_router_prompt(
                Path("/definitely/missing/router-examples.json"),
                capability_registry=registry,
            )

        self.assertIn('“列出家里的监控设备”=>home/query/camera_inventory', prompt)
        self.assertNotIn("这个候选还不能生效", prompt)
    def test_prompt_teaches_pet_activity_aliases_as_recent_camera_queries(self):
        prompt = build_router_prompt(Path("/definitely/missing/router-examples.json"))
        self.assertIn('“宠物龟在干嘛”=>camera_recent', prompt)
        self.assertIn('“乌龟现在在做什么”=>camera_recent', prompt)
        self.assertIn('“宠物仓鼠出来了吗”=>camera_recent', prompt)
        self.assertIn("宠物当前活动问法默认读取近期感知记录", prompt)
        self.assertIn("只有明确要求重新打开或实时查看摄像头", prompt)

    def test_prompt_loads_openclaw_examples_without_turn_answers(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "router-examples.json"
            path.write_text(json.dumps([{
                "request": "以后每晚看看饮水器", "route": "task",
                "intent": "create", "executor": "openclaw",
            }], ensure_ascii=False), encoding="utf-8")
            prompt = build_router_prompt(path)
        self.assertIn('“以后每晚看看饮水器”=>task/create', prompt)
        self.assertNotIn("answer", prompt)

    def test_prompt_ignores_malformed_or_oversized_examples(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "router-examples.json"
            path.write_text(json.dumps([
                {"request": "x" * 900, "route": "task", "intent": "create", "executor": "openclaw"},
                {"request": "危险", "route": "shell", "intent": "exec", "executor": "exec"},
            ], ensure_ascii=False), encoding="utf-8")
            prompt = build_router_prompt(path)
        self.assertNotIn("危险", prompt)
        self.assertLess(len(prompt), 4000)


class SemanticRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_camera_inventory_capability_comes_from_model_decision(self):
        classifier = FakeClassifier({
            "intent": "home", "operation": "query",
            "capability": "camera_inventory", "confidence": 0.99,
        })
        decision = await SemanticRouter(Router(), classifier, enabled=True).decide("家里有几台摄像头")
        self.assertEqual(decision.capability, "camera_inventory")

    async def test_unrelated_home_query_cannot_claim_camera_inventory(self):
        classifier = FakeClassifier({
            "intent": "home", "operation": "query",
            "capability": "none", "confidence": 0.99,
        })
        decision = await SemanticRouter(Router(), classifier, enabled=True).decide("空调多少度")
        self.assertEqual(decision.capability, "none")

    async def test_high_confidence_task_comes_only_from_model(self):
        classifier = FakeClassifier({"intent": "task", "operation": "create", "confidence": 0.99})
        router = SemanticRouter(Router(), classifier, enabled=True)
        decision = await router.decide("没有任何固定规则也应该服从模型")
        self.assertEqual((decision.route, decision.intent), (Route.TASK, "create"))
        self.assertEqual(decision.rule_id, "semantic_task")

    async def test_low_confidence_result_does_not_guess_semantics(self):
        classifier = FakeClassifier({"intent": "camera_recent", "operation": "query", "confidence": 0.49})
        decision = await SemanticRouter(Router(), classifier, enabled=True, min_confidence=0.8).decide("宠物仓鼠在干嘛")
        self.assertEqual(decision.route, Route.LOCAL_CHAT)
        self.assertEqual(decision.rule_id, "model_unavailable")

    async def test_invalid_classifier_result_does_not_guess_semantics(self):
        classifier = FakeClassifier({"intent": "not_allowed", "operation": "action", "confidence": 1.0})
        decision = await SemanticRouter(Router(), classifier, enabled=True).decide("打开示例房间的灯")
        self.assertEqual(decision.route, Route.LOCAL_CHAT)

    async def test_classifier_failure_is_safe_local_chat_fallback(self):
        classifier = FakeClassifier(error=ValueError("bad json"))
        decision = await SemanticRouter(Router(), classifier, enabled=True).decide("提醒家里老人每天量一次血压")
        self.assertEqual(decision.route, Route.LOCAL_CHAT)
        self.assertEqual(decision.rule_id, "model_unavailable")

    async def test_native_command_never_calls_model(self):
        classifier = FakeClassifier({"intent": "chat", "operation": "chat", "confidence": 1.0})
        decision = await SemanticRouter(Router(), classifier, enabled=True).decide("播放周杰伦")
        self.assertEqual(decision.route, Route.NATIVE)
        self.assertEqual(classifier.calls, [])

    async def test_model_operation_controls_home_and_task_risk(self):
        action = SemanticRouter(Router(), FakeClassifier({"intent": "home", "operation": "action", "confidence": 0.99}), enabled=True)
        query = SemanticRouter(Router(), FakeClassifier({"intent": "task", "operation": "list", "confidence": 0.99}), enabled=True)
        home = await action.decide("任意设备动作表达")
        task = await query.decide("任意任务查询表达")
        self.assertEqual((home.route, home.intent, home.risk), (Route.HOME, "action", "device_action"))
        self.assertEqual((task.route, task.intent, task.risk), (Route.TASK, "list", "read_only"))

    async def test_model_can_route_external_realtime_information_to_web_query(self):
        router = SemanticRouter(
            Router(),
            FakeClassifier({"intent": "web_query", "operation": "query", "confidence": 0.99}),
            enabled=True,
        )
        decision = await router.decide("任意需要外部实时信息的表达")
        self.assertEqual(
            (decision.route, decision.intent, decision.risk),
            (Route.WEB_QUERY, "query", "read_only"),
        )


if __name__ == "__main__":
    unittest.main()
