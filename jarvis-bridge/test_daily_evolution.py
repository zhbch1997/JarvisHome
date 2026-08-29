import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from capability_defaults import camera_inventory_bundle
from capability_registry import CapabilityRegistry
from daily_evolution import _valid_router_output, ollama_evaluator, run_daily


class DailyEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_daily_promotes_prompt_without_creating_legacy_capability_file(self):
        (self.root / "router-candidate-examples.json").write_text(json.dumps([
            {"request":"列出任务","route":"task","intent":"list","executor":"openclaw"}
        ]), encoding="utf-8")
        result = run_daily(self.root, lambda examples: {
            "valid_json_rate":1.0, "baseline_accuracy":1.0,
            "candidate_accuracy":1.0, "p95_seconds":0.8,
        }, capability_registry=CapabilityRegistry(self.root / "capabilities"))
        self.assertEqual(result["prompt"]["status"], "promoted")
        self.assertEqual(result["capability_candidate_count"], 0)
        self.assertFalse((self.root / "capability-candidates.json").exists())

    def test_daily_writes_complete_capability_candidate_to_registry_only(self):
        feedback = {
            "feedback_id": "feedback-1",
            "original_turn_id": "turn-1",
            "request": "列出家里的监控设备",
            "original_route": "local_chat",
            "original_intent": "chat",
            "original_executor": "local_9b",
            "original_success": True,
            "review_route": "home",
            "review_intent": "query",
            "review_executor": "external_home",
            "review_capability": "camera_inventory",
            "review_confidence": 0.99,
            "review_safe_to_retry": True,
            "review_reason": "应由摄像头目录能力处理",
        }
        (self.root / "feedback.jsonl").write_text(
            json.dumps(feedback, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        registry = CapabilityRegistry(self.root / "capabilities")
        registry.install_initial(camera_inventory_bundle())

        result = run_daily(
            self.root, evaluator=lambda _: {
                "valid_json_rate": 1.0, "baseline_accuracy": 1.0,
                "candidate_accuracy": 1.0, "p50_seconds": 0.1, "p95_seconds": 0.2,
            }, capability_registry=registry,
        )

        proposal = registry.candidate("camera_inventory")
        self.assertEqual(result["capability_candidate_count"], 1)
        self.assertEqual(proposal["capability"]["version"], 2)
        self.assertIn("列出家里的监控设备", proposal["capability"]["routing"]["examples"])
        self.assertEqual(proposal["evidence_ids"], ["turn-1"])
        self.assertFalse((self.root / "capability-candidates.json").exists())

    def test_daily_candidate_rescan_is_idempotent(self):
        feedback = {
            "feedback_id": "feedback-1", "original_turn_id": "turn-1",
            "request": "列出家里的监控设备",
            "review_route": "home", "review_intent": "query",
            "review_executor": "external_home", "review_capability": "camera_inventory",
            "review_confidence": 0.99, "review_safe_to_retry": True,
            "review_reason": "应由摄像头目录能力处理",
        }
        (self.root / "feedback.jsonl").write_text(
            json.dumps(feedback, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        registry = CapabilityRegistry(self.root / "capabilities")
        registry.install_initial(camera_inventory_bundle())
        evaluator = lambda _: {
            "valid_json_rate": 1.0, "baseline_accuracy": 1.0,
            "candidate_accuracy": 1.0, "p50_seconds": 0.1, "p95_seconds": 0.2,
        }

        run_daily(self.root, evaluator=evaluator, capability_registry=registry)
        run_daily(self.root, evaluator=evaluator, capability_registry=registry)

        proposal = registry.candidate("camera_inventory")
        self.assertEqual(proposal["capability"]["version"], 2)
        self.assertEqual(proposal["evidence_ids"], ["turn-1"])
        self.assertEqual(
            proposal["capability"]["routing"]["examples"].count("列出家里的监控设备"), 1,
        )

    def test_daily_rejects_inconsistent_capability_review(self):
        feedback = {
            "feedback_id": "feedback-invalid",
            "original_turn_id": "turn-invalid",
            "request": "列出家里的监控设备",
            "review_route": "home", "review_intent": "query",
            "review_executor": "local_9b", "review_capability": "camera_inventory",
            "review_confidence": 0.99, "review_safe_to_retry": True,
            "review_reason": "错误的执行者组合",
        }
        (self.root / "feedback.jsonl").write_text(
            json.dumps(feedback, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        registry = CapabilityRegistry(self.root / "capabilities")
        registry.install_initial(camera_inventory_bundle())

        result = run_daily(
            self.root, evaluator=lambda _: {
                "valid_json_rate": 1.0, "baseline_accuracy": 1.0,
                "candidate_accuracy": 1.0, "p50_seconds": 0.1, "p95_seconds": 0.2,
            }, capability_registry=registry,
        )

        self.assertEqual(result["capability_candidate_count"], 0)
        self.assertIsNone(registry.candidate("camera_inventory"))

    def test_capability_none_feedback_never_becomes_registry_candidate(self):
        row = {
            "feedback_id": "f1", "original_turn_id": "turn-1",
            "request": "打开灯", "review_route": "home",
            "review_intent": "action", "review_executor": "openclaw",
            "review_capability": "none", "review_confidence": 0.99,
            "review_safe_to_retry": False, "review_reason": "设备控制交给OpenClaw",
        }
        (self.root / "feedback.jsonl").write_text(
            json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        registry = CapabilityRegistry(self.root / "capabilities")
        result = run_daily(
            self.root, evaluator=lambda _: {
                "valid_json_rate": 1.0, "baseline_accuracy": 1.0,
                "candidate_accuracy": 1.0, "p50_seconds": 0.1, "p95_seconds": 0.2,
            }, capability_registry=registry,
        )
        self.assertEqual(result["capability_candidate_count"], 0)
        self.assertEqual(registry.list_candidates(), [])

    def test_evaluator_accepts_only_current_capability_router_schema(self):
        self.assertTrue(_valid_router_output({
            "intent": "home", "operation": "query",
            "capability": "camera_inventory", "confidence": 0.99,
        }))
        self.assertFalse(_valid_router_output({
            "intent": "home", "operation": "query", "confidence": 0.99,
        }))
        self.assertFalse(_valid_router_output({
            "intent": "home", "operation": "query", "capability": "none",
            "confidence": 0.99, "unexpected": True,
        }))

    def test_ollama_evaluator_uses_dedicated_router_and_active_capability_prompt(self):
        feedback_root = self.root / "feedback"
        capability_root = self.root / "capabilities"
        registry = CapabilityRegistry(capability_root)
        bundle = camera_inventory_bundle()
        bundle["routing"]["examples"] = ["运行时能力表达"]
        registry.install_initial(bundle)
        requests = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "response": json.dumps({
                        "intent": "chat", "operation": "chat",
                        "capability": "none", "confidence": 0.99,
                    })
                }).encode()

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            return FakeResponse()

        env = {
            "JARVIS_FEEDBACK_STATE": str(feedback_root),
            "JARVIS_CAPABILITY_STATE": str(capability_root),
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "daily_evolution.urllib.request.urlopen", side_effect=fake_urlopen,
        ):
            metrics = ollama_evaluator([])

        payload = json.loads(requests[0][0].data)
        self.assertEqual(requests[0][0].full_url, "http://127.0.0.1:11435/api/generate")
        self.assertIn("运行时能力表达", payload["system"])
        self.assertEqual(metrics["valid_json_rate"], 1.0)


if __name__ == "__main__":
    unittest.main()
