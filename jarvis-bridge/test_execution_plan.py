import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from capability_defaults import camera_inventory_bundle, home_device_action_bundle, home_scene_action_bundle
from capability_registry import CapabilityRegistry, CapabilityValidationError
from arbitration import ArbitrationEnvelope, Level1Decision, Level2Decision
from execution_plan import (
    resolve_arbitration_plan, resolve_execution_plan, resolve_fallback_plan,
)
from router import Route, RouteDecision


class ExecutionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = CapabilityRegistry(Path(self.temp.name))
        self.registry.install_initial(camera_inventory_bundle())

    def test_capability_bundle_is_single_source_for_executor_trace_and_delivery(self):
        decision = RouteDecision(
            Route.HOME, "query", "semantic_home", "read_only", 0.99,
            capability="camera_inventory",
        )
        plan = resolve_execution_plan(decision, self.registry)
        self.assertEqual(plan.capability, "camera_inventory")
        self.assertEqual(plan.capability_version, 1)
        self.assertEqual(plan.executor, "bridge_recipe")
        self.assertEqual(plan.producer, "external_home")
        self.assertEqual(plan.phase, "execution")
        self.assertEqual(plan.transition, "好的主人，我查一下。")
        self.assertEqual(plan.progress, "我正在读取设备目录。")

    def test_capability_route_operation_mismatch_fails_closed(self):
        decision = RouteDecision(
            Route.HOME, "action", "semantic_home", "device_action", 0.99,
            capability="camera_inventory",
        )
        with self.assertRaises(CapabilityValidationError):
            resolve_execution_plan(decision, self.registry)

    def test_unknown_capability_fails_closed_instead_of_falling_back_to_openclaw(self):
        decision = RouteDecision(
            Route.HOME, "query", "semantic_home", "read_only", 0.99,
            capability="unknown_capability",
        )
        with self.assertRaises(CapabilityValidationError):
            resolve_execution_plan(decision, self.registry)

    def test_declared_fallback_to_unrestricted_openclaw_is_fail_closed(self):
        decision = RouteDecision(
            Route.HOME, "query", "semantic_home", "read_only", 0.99,
            capability="camera_inventory",
        )
        plan = resolve_execution_plan(decision, self.registry)

        with self.assertRaisesRegex(
            CapabilityValidationError, "restricted fallback profile is unavailable",
        ):
            resolve_fallback_plan(
                plan, reason="data_source_unavailable", fallback_allowed=True,
            )

    def test_programming_error_cannot_use_declared_fallback(self):
        decision = RouteDecision(
            Route.HOME, "query", "semantic_home", "read_only", 0.99,
            capability="camera_inventory",
        )
        plan = resolve_execution_plan(decision, self.registry)
        with self.assertRaises(CapabilityValidationError):
            resolve_fallback_plan(plan, reason="recipe_invalid", fallback_allowed=False)

    def test_non_capability_routes_keep_safe_existing_ownership(self):
        cases = [
            (Route.CAMERA, "camera_recent", "external_home", "external_home", "execution"),
            (Route.LOCAL_CHAT, "chat", "local_4b", "local_4b", "generation"),
            (Route.HOME, "query", "openclaw", "openclaw", "planning"),
            (Route.TASK, "create", "openclaw", "openclaw", "planning"),
        ]
        for route, intent, executor, producer, phase in cases:
            with self.subTest(route=route, intent=intent):
                plan = resolve_execution_plan(RouteDecision(route, intent, "test"), self.registry)
                self.assertEqual((plan.executor, plan.producer, plan.phase), (executor, producer, phase))

    def envelope(self, l1, tool="none", handoff="none", l2=None):
        return ArbitrationEnvelope(
            arbitration_id="arb-test",
            level1=Level1Decision(l1, tool, handoff, 0.99),
            level2_required=l1 == "handoff",
            level2=l2,
        )

    def make_selectable(self, tier):
        bundle = camera_inventory_bundle()
        bundle["selector"] = {
            "tier": tier, "description": "查询家庭摄像头目录",
            "positive_examples": ["有哪些摄像头"],
            "dangerous_negatives": ["现在打开摄像头"],
            "min_confidence": {"0.8b": 0.99, "4b": 0.95},
        }
        bundle["taxonomy"] = {
            "domain_tags": ["home", "camera"],
            "execution_kind": "deterministic_query",
        }
        other = CapabilityRegistry(Path(self.temp.name) / f"{tier}-registry")
        other.install_initial(bundle)
        return other

    def test_level1_quick_tool_plan_uses_active_bundle_and_public_quick_tool_class(self):
        registry = self.make_selectable("08b_eligible")
        plan = resolve_arbitration_plan(
            self.envelope("quick_tool", "camera_inventory", "lookup"), registry,
        )
        self.assertEqual(plan.arbitration_id, "arb-test")
        self.assertEqual(plan.decision_tier, "0.8b")
        self.assertEqual(plan.execution_class, "quick_tool")
        self.assertEqual(plan.tool_class, "deterministic_query")
        self.assertEqual(plan.selector_tier, "08b_eligible")
        self.assertEqual((plan.executor, plan.producer), ("bridge_recipe", "external_home"))

    def test_level2_quick_tool_plan_is_child_of_same_arbitration(self):
        registry = self.make_selectable("4b_eligible")
        level2 = Level2Decision("quick_tool", "camera_inventory", "consistent", 0.98)
        plan = resolve_arbitration_plan(
            self.envelope("handoff", handoff="lookup", l2=level2), registry,
        )
        self.assertEqual(plan.arbitration_id, "arb-test")
        self.assertEqual(plan.decision_tier, "4b")
        self.assertEqual(plan.execution_class, "quick_tool")
        self.assertEqual(plan.capability, "camera_inventory")

    def test_device_action_quick_tool_plan_truthfully_uses_local_4b_device_executor(self):
        registry = CapabilityRegistry(Path(self.temp.name) / "device-action")
        registry.install_initial(home_device_action_bundle())
        level2 = Level2Decision("quick_tool", "home_device_action", "consistent", 0.99)
        plan = resolve_arbitration_plan(
            self.envelope("handoff", handoff="action", l2=level2), registry,
        )
        self.assertEqual(plan.execution_class, "quick_tool")
        self.assertEqual(plan.executor, "local_4b_device")
        self.assertEqual(plan.producer, "local_4b")
        self.assertEqual(plan.tool_class, "device_action")

    def test_scene_action_quick_tool_plan_truthfully_uses_local_4b_scene_executor(self):
        registry = CapabilityRegistry(Path(self.temp.name) / "scene-action")
        registry.install_initial(home_scene_action_bundle())
        level2 = Level2Decision("quick_tool", "home_scene_action", "consistent", 0.99)
        plan = resolve_arbitration_plan(
            self.envelope("handoff", handoff="action", l2=level2), registry,
        )
        self.assertEqual(plan.executor, "local_4b_scene")
        self.assertEqual(plan.producer, "local_4b")
        self.assertEqual(plan.tool_class, "scene_action")

    def test_level2_openclaw_and_level1_chat_get_truthful_execution_classes(self):
        openclaw = resolve_arbitration_plan(
            self.envelope(
                "handoff", handoff="general",
                l2=Level2Decision("openclaw", "none", "escalated", 0.97),
            ), self.registry,
        )
        chat = resolve_arbitration_plan(self.envelope("chat"), self.registry)
        self.assertEqual(
            (openclaw.decision_tier, openclaw.execution_class, openclaw.producer),
            ("openclaw", "openclaw", "openclaw"),
        )
        self.assertEqual(
            (chat.decision_tier, chat.execution_class, chat.producer),
            ("0.8b", "local_chat", "local_4b"),
        )

    def test_incomplete_or_inconsistent_envelope_fails_closed(self):
        invalid = self.envelope("handoff", handoff="lookup", l2=None)
        with self.assertRaises(CapabilityValidationError):
            resolve_arbitration_plan(invalid, self.registry)
        wrong_tier = self.make_selectable("4b_eligible")
        with self.assertRaises(CapabilityValidationError):
            resolve_arbitration_plan(
                self.envelope("quick_tool", "camera_inventory", "lookup"), wrong_tier,
            )


if __name__ == "__main__":
    unittest.main()
