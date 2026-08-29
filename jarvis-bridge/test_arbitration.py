import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from arbitration import (
    Level1Arbitrator, Level2Arbitrator,
    OllamaLevel1Classifier, OllamaLevel2Classifier,
    build_level1_prompt, build_level2_prompt,
    level1_output_schema, level2_output_schema,
)
from capability_registry import CapabilityRegistry
from capability_defaults import home_device_action_bundle, home_scene_action_bundle
from test_capability_registry import CAMERA


class FakeClassifier:
    def __init__(self, result):
        self.result = result

    async def classify(self, _text):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeLevel2Classifier(FakeClassifier):
    def __init__(self, result):
        super().__init__(result)
        self.calls = []

    async def classify(self, text, envelope):
        self.calls.append((text, envelope))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class Level1ArbitratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = CapabilityRegistry(Path(self.temp.name))

    def install(self, tier="08b_eligible"):
        bundle = json.loads(json.dumps(CAMERA))
        bundle["selector"] = {
            "tier": tier, "description": "查询家庭摄像头清单",
            "positive_examples": ["家里有几台摄像头"],
            "dangerous_negatives": ["现在打开摄像头"],
            "min_confidence": {"0.8b": 0.99, "4b": 0.95},
        }
        bundle["taxonomy"] = {
            "domain_tags": ["home", "camera"],
            "execution_kind": "deterministic_query",
        }
        self.registry.install_initial(bundle)

    async def decide(self, result, threshold=0.9, text="测试请求"):
        return await Level1Arbitrator(
            FakeClassifier(result), self.registry, min_confidence=threshold,
        ).decide(text)

    async def test_allowed_air_conditioner_sleep_scene_is_never_misclassified_as_chat(self):
        result = await self.decide({
            "decision": "chat", "quick_tool_id": "none",
            "handoff": "none", "confidence": 0.99,
        }, text="空调睡眠24度，仅限主卧")
        self.assertEqual(result.level1.decision, "handoff")
        self.assertEqual(result.level1.handoff, "action")
        self.assertTrue(result.level2_required)

    def test_level1_prompt_and_schema_only_expose_08b_eligible_tools(self):
        self.install("08b_eligible")
        other = json.loads(json.dumps(CAMERA))
        other["id"] = "pet_recent_activity"
        other["execution"]["recipe"] = "pet_recent_activity_v1"
        other["selector"] = {
            "tier": "4b_eligible", "description": "读取宠物近期活动",
            "positive_examples": ["宠物仓鼠最近在干嘛"],
            "dangerous_negatives": ["现在打开摄像头"],
            "min_confidence": {"0.8b": 0.99, "4b": 0.95},
        }
        other["taxonomy"] = {
            "domain_tags": ["camera", "pet_care"],
            "execution_kind": "deterministic_query",
        }
        self.registry.install_initial(other)

        prompt = build_level1_prompt(self.registry)
        schema = level1_output_schema(self.registry)
        self.assertIn("camera_inventory", prompt)
        self.assertNotIn("pet_recent_activity", prompt)
        self.assertEqual(
            schema["properties"]["quick_tool_id"]["enum"],
            ["camera_inventory", "none"],
        )
        branches = schema["oneOf"]
        self.assertEqual(
            [branch["properties"]["decision"]["const"] for branch in branches],
            ["chat", "quick_tool", "handoff"],
        )
        self.assertTrue(all(
            branch["required"] == [
                "decision", "quick_tool_id", "handoff", "confidence",
            ]
            and "confidence" in branch["properties"]
            and branch["additionalProperties"] is False
            for branch in branches
        ))
        self.assertEqual(
            branches[1]["properties"]["quick_tool_id"]["enum"],
            ["camera_inventory"],
        )
        self.assertEqual(
            branches[2]["properties"]["handoff"]["enum"],
            ["lookup", "analyze", "action", "general"],
        )
        self.assertIn("chat 的 handoff 必须是 none", prompt)
        self.assertIn("查询今天北京天气", prompt)
        self.assertIn("家里有几台摄像头", prompt)
        self.assertIn("家庭实时事实", prompt)
        self.assertIn("看看宠物仓鼠在干嘛", prompt)
        self.assertIn("宠物当前位置、活动或状态", prompt)
        self.assertIn("不依赖这个家庭当前情况也能恒真回答", prompt)
        self.assertIn("一般原理、知识和习性仍用chat", prompt)
        self.assertNotIn("routing", prompt)
        self.assertNotIn("allowed_tools", prompt)

    def test_level1_prompt_loads_only_v2_active_examples(self):
        self.install("08b_eligible")
        examples = Path(self.temp.name) / "level1-active-examples.json"
        examples.write_text(json.dumps([
            {
                "request": "给我讲个笑话", "decision": "chat",
                "quick_tool_id": "none", "handoff": "none",
                "evidence_ids": ["turn-1"],
            },
            {
                "request": "旧格式", "route": "local_chat",
                "intent": "chat", "executor": "local_4b",
            },
        ]), encoding="utf-8")
        prompt = build_level1_prompt(self.registry, examples_path=examples)
        self.assertIn("给我讲个笑话", prompt)
        self.assertIn("chat/none/none", prompt)
        self.assertNotIn("旧格式", prompt)
        self.assertNotIn("executor", prompt)

    def test_level1_prompt_ignores_active_quick_tool_after_tier_downgrade(self):
        self.install("4b_eligible")
        examples = Path(self.temp.name) / "level1-active-examples.json"
        examples.write_text(json.dumps([{
            "request": "家里有几台摄像头", "decision": "quick_tool",
            "quick_tool_id": "camera_inventory", "handoff": "lookup",
            "evidence_ids": ["turn-1"],
        }]), encoding="utf-8")
        prompt = build_level1_prompt(self.registry, examples_path=examples)
        self.assertNotIn("家里有几台摄像头”=>quick_tool", prompt)

    async def test_ollama_level1_classifier_uses_dynamic_contract_and_disables_thinking(self):
        self.install("08b_eligible")
        captured = {}

        def handler(request):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"response": json.dumps({
                "decision": "handoff", "quick_tool_id": "none",
                "handoff": "lookup", "confidence": 0.98,
            })})

        classifier = OllamaLevel1Classifier(
            self.registry, transport=httpx.MockTransport(handler),
        )
        result = await classifier.classify("查一下今天的天气")

        self.assertEqual(result["decision"], "handoff")
        self.assertEqual(captured["model"], "qwen35-router:0.8b")
        self.assertIs(captured["think"], False)
        self.assertEqual(captured["format"], level1_output_schema(self.registry))
        self.assertIn("一级仲裁器", captured["system"])
        self.assertNotIn("operation", captured["system"])
        self.assertNotIn("capability", captured["system"])

    async def test_chat_creates_terminal_parent_envelope(self):
        envelope = await self.decide({
            "decision": "chat", "quick_tool_id": "none",
            "handoff": "none", "confidence": 0.98,
        })
        self.assertEqual(envelope.level1.decision, "chat")
        self.assertFalse(envelope.level2_required)
        self.assertTrue(envelope.arbitration_id.startswith("arb-"))

    async def test_handoff_requires_level2_and_preserves_transition_class(self):
        envelope = await self.decide({
            "decision": "handoff", "quick_tool_id": "none",
            "handoff": "lookup", "confidence": 0.97,
        })
        self.assertEqual(envelope.level1.handoff, "lookup")
        self.assertTrue(envelope.level2_required)

    async def test_level1_can_only_choose_08b_eligible_tool(self):
        self.install("08b_eligible")
        envelope = await self.decide({
            "decision": "quick_tool", "quick_tool_id": "camera_inventory",
            "handoff": "lookup", "confidence": 0.995,
        })
        self.assertEqual(envelope.level1.quick_tool_id, "camera_inventory")
        self.assertFalse(envelope.level2_required)

    async def test_level1_exact_quick_tool_match_is_registry_driven_and_skips_model(self):
        self.install("08b_eligible")
        classifier = FakeClassifier(AssertionError("model must not run for exact match"))
        envelope = await Level1Arbitrator(
            classifier, self.registry, min_confidence=0.9,
        ).decide("  家里有几台摄像头？ ")
        self.assertEqual(envelope.level1.decision, "quick_tool")
        self.assertEqual(envelope.level1.quick_tool_id, "camera_inventory")
        self.assertEqual(envelope.level1.handoff, "lookup")
        self.assertEqual(envelope.level1.confidence, 1.0)

    async def test_level1_exact_match_does_not_use_dangerous_negative_or_partial_text(self):
        self.install("08b_eligible")
        classifier = FakeClassifier({
            "decision": "handoff", "quick_tool_id": "none",
            "handoff": "lookup", "confidence": 0.99,
        })
        for text in ("现在打开摄像头", "请问家里有几台摄像头并打开它们"):
            envelope = await Level1Arbitrator(
                classifier, self.registry, min_confidence=0.9,
            ).decide(text)
            self.assertEqual(envelope.level1.decision, "handoff")
            self.assertEqual(envelope.level1.quick_tool_id, "none")

    async def test_unknown_or_ineligible_tool_fails_safe_to_handoff(self):
        self.install("4b_eligible")
        for tool_id in ("camera_inventory", "invented_tool"):
            envelope = await self.decide({
                "decision": "quick_tool", "quick_tool_id": tool_id,
                "handoff": "lookup", "confidence": 0.999,
            })
            self.assertEqual(envelope.level1.decision, "handoff")
            self.assertEqual(envelope.level1.handoff, "general")
            self.assertTrue(envelope.level2_required)

    async def test_invalid_low_confidence_or_classifier_failure_fails_safe(self):
        results = [
            {"decision": "chat", "quick_tool_id": "camera_inventory", "handoff": "none", "confidence": 0.99},
            {"decision": "chat", "quick_tool_id": "none", "handoff": "none", "confidence": 0.2},
            ValueError("bad json"),
        ]
        for result in results:
            envelope = await self.decide(result)
            self.assertEqual(envelope.level1.decision, "handoff")
            self.assertEqual(envelope.level1.handoff, "general")
            self.assertTrue(envelope.level2_required)


class Level2ArbitratorTests(Level1ArbitratorTests):
    async def parent(self, handoff="lookup"):
        return await self.decide({
            "decision": "handoff", "quick_tool_id": "none",
            "handoff": handoff, "confidence": 0.98,
        })

    async def test_level2_refines_parent_to_4b_eligible_quick_tool(self):
        self.install("4b_eligible")
        parent = await self.parent("lookup")
        result = await Level2Arbitrator(FakeLevel2Classifier({
            "decision": "quick_tool", "quick_tool_id": "camera_inventory",
            "confidence": 0.98,
        }), self.registry).decide("测试", parent)
        self.assertEqual(result.arbitration_id, parent.arbitration_id)
        self.assertEqual(result.level2.decision, "quick_tool")
        self.assertEqual(result.level2.quick_tool_id, "camera_inventory")
        self.assertEqual(result.level2.relation, "consistent")

    async def test_level2_general_parent_records_override(self):
        self.install("4b_eligible")
        result = await Level2Arbitrator(FakeLevel2Classifier({
            "decision": "quick_tool", "quick_tool_id": "camera_inventory",
            "confidence": 0.98,
        }), self.registry).decide("测试", await self.parent("general"))
        self.assertEqual(result.level2.relation, "overridden")

    async def test_action_parent_cannot_be_downgraded_to_read_only_quick_tool(self):
        self.install("4b_eligible")
        result = await Level2Arbitrator(FakeLevel2Classifier({
            "decision": "quick_tool", "quick_tool_id": "camera_inventory",
            "confidence": 0.99,
        }), self.registry).decide("测试", await self.parent("action"))
        self.assertEqual(result.level2.decision, "openclaw")
        self.assertEqual(result.level2.relation, "escalated")

    async def test_allowed_scene_family_selects_registered_scene_quick_tool_even_when_model_escalates(self):
        self.registry.install_initial(home_scene_action_bundle())
        result = await Level2Arbitrator(FakeLevel2Classifier({
            "decision": "openclaw", "quick_tool_id": "none", "confidence": 0.99,
        }), self.registry).decide("空调睡眠24度，仅限主卧", await self.parent("action"))
        self.assertEqual(result.level2.decision, "quick_tool")
        self.assertEqual(result.level2.quick_tool_id, "home_scene_action")

    async def test_action_parent_can_select_registered_device_action_quick_tool(self):
        self.registry.install_initial(home_device_action_bundle())
        result = await Level2Arbitrator(FakeLevel2Classifier({
            "decision": "quick_tool", "quick_tool_id": "home_device_action",
            "confidence": 0.99,
        }), self.registry).decide("打开客厅灯", await self.parent("action"))
        self.assertEqual(result.level2.decision, "quick_tool")
        self.assertEqual(result.level2.quick_tool_id, "home_device_action")

    async def test_invalid_unknown_or_low_confidence_level2_escalates(self):
        self.install("4b_eligible")
        for model_result in (
            {"decision": "quick_tool", "quick_tool_id": "invented", "confidence": 1.0},
            {"decision": "quick_tool", "quick_tool_id": "camera_inventory", "confidence": 0.2},
            ValueError("bad json"),
        ):
            result = await Level2Arbitrator(
                FakeLevel2Classifier(model_result), self.registry,
            ).decide("测试", await self.parent())
            self.assertEqual(result.level2.decision, "openclaw")
            self.assertEqual(result.level2.relation, "escalated")

    async def test_level2_handoff_phrase_does_not_override_capability_permissions(self):
        self.registry.install_initial(home_device_action_bundle())
        for handoff in ("general", "lookup", "analyze", "action"):
            result = await Level2Arbitrator(FakeLevel2Classifier({
                "decision": "quick_tool", "quick_tool_id": "home_device_action",
                "confidence": 0.99,
            }), self.registry).decide(
                "打开示例房间的米家智能显示器挂灯1S",
                await self.parent(handoff),
            )
            self.assertIsNotNone(result.level2)
            assert result.level2 is not None
            self.assertEqual(result.level2.decision, "quick_tool")
            self.assertEqual(result.level2.quick_tool_id, "home_device_action")

    async def test_level2_rejects_terminal_level1_parent(self):
        chat_parent = await self.decide({
            "decision": "chat", "quick_tool_id": "none",
            "handoff": "none", "confidence": 0.99,
        })
        with self.assertRaises(ValueError):
            await Level2Arbitrator(FakeLevel2Classifier({
                "decision": "openclaw", "quick_tool_id": "none", "confidence": 1.0,
            }), self.registry).decide("测试", chat_parent)

    async def test_ollama_level2_contract_contains_parent_and_only_l2_tools(self):
        self.install("4b_eligible")
        captured = {}

        def handler(request):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"response": json.dumps({
                "decision": "openclaw", "quick_tool_id": "none", "confidence": 0.97,
            })})

        parent = await self.parent("analyze")
        classifier = OllamaLevel2Classifier(
            self.registry, transport=httpx.MockTransport(handler),
        )
        await classifier.classify("比较这些数据", parent)
        self.assertEqual(captured["model"], "qwen35-4b-16k:latest")
        self.assertIs(captured["think"], False)
        self.assertIn(parent.arbitration_id, captured["system"])
        self.assertIn("handoff=analyze", captured["system"])
        self.assertEqual(captured["format"], level2_output_schema(self.registry))
        self.assertEqual(captured["options"]["seed"], 0)
        self.assertIn("camera_inventory", build_level2_prompt(self.registry, parent))


if __name__ == "__main__":
    unittest.main()
