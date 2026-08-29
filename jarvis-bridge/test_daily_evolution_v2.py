import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from capability_defaults import camera_inventory_quick_bundle
from capability_registry import CapabilityRegistry
from daily_evolution_v2 import (
    Level1Evolution,
    collect_level1_candidates,
    ollama_level1_evaluator,
    evolution_lock,
    discover_quick_tool_drafts,
    discover_openclaw_patterns,
    parse_discovery_groups,
    propose_capability_tier_changes,
    propose_observed_capabilities,
    run_production_daily,
    run_daily_v2,
)


class DailyEvolutionV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.feedback = self.root / "route-feedback"
        self.registry = CapabilityRegistry(self.root / "capabilities")
        self.registry.install_initial(camera_inventory_quick_bundle())

    def write_turns(self, rows):
        self.feedback.mkdir(parents=True, exist_ok=True)
        (self.feedback / "turns.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )

    def test_collects_only_explicitly_reviewed_current_level1_contract(self):
        self.feedback.mkdir(parents=True, exist_ok=True)
        reviews = self.feedback / "evolution-feedback-v2.jsonl"
        reviews.write_text(json.dumps({
            "review_id": "review-1", "turn_id": "turn-1", "request": "讲个笑话",
            "level1": {"decision": "chat", "quick_tool_id": "none", "handoff": "none"},
            "level2": None, "confidence": 0.99, "source": "webui_explicit_review",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        candidates = collect_level1_candidates(reviews)
        self.assertEqual(candidates, [{
            "request": "讲个笑话", "decision": "chat",
            "quick_tool_id": "none", "handoff": "none",
            "evidence_ids": ["turn-1"],
        }])
        self.assertNotIn("route", candidates[0])
        self.assertNotIn("executor", candidates[0])

    def test_unreviewed_successful_turn_never_self_trains_level1(self):
        self.write_turns([{
            "turn_id": "turn-1", "request": "讲个笑话", "success": True,
            "level1": {"decision": "chat", "quick_tool_id": "none", "handoff": "none", "confidence": 0.99},
            "level2": None, "tool_trace": [], "risk_class": "unrecorded",
        }])
        self.assertEqual(collect_level1_candidates(self.feedback / "turns.jsonl"), [])

    def test_active_review_is_not_reintroduced_as_candidate(self):
        self.feedback.mkdir(parents=True, exist_ok=True)
        active = [{
            "request": "讲个笑话", "decision": "chat",
            "quick_tool_id": "none", "handoff": "none",
            "evidence_ids": ["turn-old"],
        }]
        (self.feedback / "level1-active-examples.json").write_text(
            json.dumps(active, ensure_ascii=False), encoding="utf-8",
        )
        (self.feedback / "evolution-feedback-v2.jsonl").write_text(
            json.dumps({
                "turn_id": "turn-old", "request": "讲个笑话",
                "level1": {"decision": "chat", "quick_tool_id": "none", "handoff": "none"},
                "source": "webui_explicit_review",
            }, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        called = []
        result = run_daily_v2(
            self.feedback, self.registry,
            evaluator=lambda _: called.append(True) or {},
        )
        self.assertEqual(result["level1"]["status"], "skipped")
        self.assertEqual(called, [])

    def test_conflicting_reviews_stay_quarantined_after_later_duplicate(self):
        self.feedback.mkdir(parents=True, exist_ok=True)
        rows = []
        for turn, decision, handoff in (
            ("t1", "chat", "none"),
            ("t2", "handoff", "general"),
            ("t3", "chat", "none"),
        ):
            rows.append(json.dumps({
                "turn_id": turn, "request": "边界问题",
                "level1": {
                    "decision": decision, "quick_tool_id": "none", "handoff": handoff,
                },
                "source": "webui_explicit_review",
            }, ensure_ascii=False))
        path = self.feedback / "evolution-feedback-v2.jsonl"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.assertEqual(collect_level1_candidates(path), [])

    def test_low_confidence_review_never_becomes_candidate(self):
        self.feedback.mkdir(parents=True, exist_ok=True)
        path = self.feedback / "evolution-feedback-v2.jsonl"
        path.write_text(json.dumps({
            "turn_id": "low-1", "request": "讲个笑话", "confidence": 0.94,
            "level1": {"decision": "chat", "quick_tool_id": "none", "handoff": "none"},
            "source": "webui_explicit_review",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(collect_level1_candidates(path), [])

    def test_failed_or_untrusted_quick_tool_turns_never_train_level1(self):
        self.write_turns([
            {
                "turn_id": "failed", "request": "摄像头清单", "success": False,
                "level1": {"decision": "quick_tool", "quick_tool_id": "camera_inventory_quick", "handoff": "lookup", "confidence": 1.0},
                "tool_trace": [{"provider": "miloco", "tool": "miloco.device_list", "status": "failed"}],
            },
            {
                "turn_id": "no-proof", "request": "有几个摄像头", "success": True,
                "level1": {"decision": "quick_tool", "quick_tool_id": "camera_inventory_quick", "handoff": "lookup", "confidence": 1.0},
                "tool_trace": [],
            },
        ])
        self.assertEqual(collect_level1_candidates(self.feedback / "turns.jsonl"), [])

    def test_legacy_candidates_are_archived_and_never_promoted(self):
        self.feedback.mkdir(parents=True, exist_ok=True)
        legacy = self.feedback / "router-candidate-examples.json"
        legacy.write_text(json.dumps([{
            "request": "打开灯", "route": "home", "intent": "action", "executor": "openclaw",
        }]), encoding="utf-8")
        result = run_daily_v2(
            self.feedback, self.registry,
            evaluator=lambda _: {"schema_rate": 1.0, "accuracy": 1.0, "dangerous_negative_accuracy": 1.0, "p95_seconds": 0.1},
        )
        self.assertEqual(result["level1"]["status"], "skipped")
        self.assertFalse(legacy.exists())
        archived = list((self.feedback / "legacy-archive").glob("router-candidate-examples-*.json"))
        self.assertEqual(len(archived), 1)
        self.assertFalse((self.feedback / "level1-active-examples.json").exists())

    def test_level1_promotion_requires_all_current_gates(self):
        evolution = Level1Evolution(self.feedback)
        evolution.write_candidates([{
            "request": "讲个笑话", "decision": "chat", "quick_tool_id": "none",
            "handoff": "none", "evidence_ids": ["turn-1"],
        }])
        rejected = evolution.promote_if_safe(lambda _: {
            "schema_rate": 1.0, "accuracy": 1.0,
            "dangerous_negative_accuracy": 0.99, "p95_seconds": 0.1,
        })
        self.assertEqual(rejected["status"], "rejected")
        self.assertFalse(evolution.active.exists())
        promoted = evolution.promote_if_safe(lambda _: {
            "schema_rate": 1.0, "accuracy": 1.0,
            "dangerous_negative_accuracy": 1.0, "p95_seconds": 0.1,
        })
        self.assertEqual(promoted["status"], "promoted")
        active = json.loads(evolution.active.read_text())
        self.assertEqual(active[0]["decision"], "chat")

    def test_daily_may_propose_but_never_promotes_capability(self):
        active_before = self.registry.active("camera_inventory_quick")
        self.write_turns([{
            "turn_id": "turn-cap", "request": "列一下监控设备", "success": True,
            "level1": {"decision": "handoff", "quick_tool_id": "none", "handoff": "lookup", "confidence": 0.99},
            "level2": {"decision": "quick_tool", "quick_tool_id": "camera_inventory_quick", "relation": "consistent", "confidence": 0.99},
            "tool_trace": [{"provider": "miloco", "tool": "miloco.device_list", "status": "completed"}],
            "risk_class": "read_only",
        }])
        result = run_daily_v2(
            self.feedback, self.registry,
            evaluator=lambda _: {"schema_rate": 1.0, "accuracy": 1.0, "dangerous_negative_accuracy": 1.0, "p95_seconds": 0.1},
        )
        self.assertEqual(result["capability_candidate_count"], 1)
        self.assertEqual(self.registry.active("camera_inventory_quick")["version"], active_before["version"])
        self.assertEqual(self.registry.candidate("camera_inventory_quick")["status"], "proposed")

    def test_capability_upgrade_needs_verified_bound_evidence_and_only_proposes(self):
        registry = CapabilityRegistry(self.root / "tier-upgrade")
        active = camera_inventory_quick_bundle()
        active["selector"]["tier"] = "openclaw_only"
        registry.install_initial(active)
        requests = ["家里有几台摄像头", "有哪些摄像头", "摄像头清单", "家里有几台摄像头", "有哪些摄像头"]
        turns = [{
            "turn_id": f"ok-{index}", "capability": "camera_inventory_quick",
            "capability_version": 1, "success": True,
            "selector_tier": "openclaw_only", "risk_class": "read_only",
            "time": f"2026-07-{29 + (index % 2):02d}T08:00:00+08:00",
            "request": requests[index],
            "execution_class": "quick_tool",
            "tool_trace": [{
                "provider": "miloco", "tool": "miloco.device_list", "status": "completed",
            }],
        } for index in range(5)]
        proposals = propose_capability_tier_changes(turns, registry)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["target_tier"], "4b_eligible")
        self.assertEqual(registry.active("camera_inventory_quick")["selector"]["tier"], "openclaw_only")

    def test_capability_upgrade_rejects_failure_or_unverified_evidence(self):
        registry = CapabilityRegistry(self.root / "tier-reject")
        active = camera_inventory_quick_bundle()
        active["selector"]["tier"] = "openclaw_only"
        registry.install_initial(active)
        turns = [{
            "turn_id": f"bad-{index}", "capability": "camera_inventory_quick",
            "capability_version": 1, "success": index != 0,
            "selector_tier": "openclaw_only", "risk_class": "read_only",
            "execution_class": "quick_tool",
            "tool_trace": [] if index == 1 else [{
                "provider": "miloco", "tool": "miloco.device_list",
                "status": "failed" if index == 0 else "completed",
            }],
        } for index in range(30)]
        self.assertEqual(propose_capability_tier_changes(turns, registry), [])
        self.assertIsNone(registry.candidate("camera_inventory_quick"))

    def test_capability_failure_only_proposes_safety_downgrade(self):
        registry = CapabilityRegistry(self.root / "tier-downgrade")
        active = camera_inventory_quick_bundle()
        active["selector"]["tier"] = "08b_eligible"
        registry.install_initial(active)
        proposals = propose_capability_tier_changes([{
            "turn_id": "incident-1", "capability": "camera_inventory_quick",
            "capability_version": 1, "success": False,
            "selector_tier": "08b_eligible", "risk_class": "read_only",
            "execution_class": "quick_tool",
            "tool_trace": [{
                "provider": "miloco", "tool": "miloco.device_list", "status": "failed",
            }],
        }], registry)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["target_tier"], "openclaw_only")
        self.assertEqual(registry.active("camera_inventory_quick")["selector"]["tier"], "08b_eligible")

    def test_existing_general_candidate_cannot_block_safety_downgrade(self):
        registry = CapabilityRegistry(self.root / "tier-incident-priority")
        active = camera_inventory_quick_bundle()
        active["selector"]["tier"] = "08b_eligible"
        registry.install_initial(active)
        ordinary = json.loads(json.dumps(active))
        ordinary["version"] = 2
        ordinary["routing"]["examples"].append("普通候选")
        registry.propose(ordinary, ["ordinary"])
        proposals = propose_capability_tier_changes([{
            "turn_id": "incident-priority", "capability": "camera_inventory_quick",
            "capability_version": 1, "success": False,
            "selector_tier": "08b_eligible", "risk_class": "read_only",
            "execution_class": "quick_tool",
            "tool_trace": [{
                "provider": "miloco", "tool": "miloco.device_list", "status": "failed",
            }],
        }], registry)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(registry.candidate("camera_inventory_quick")["target_tier"], "openclaw_only")

    def test_duplicate_or_empty_turn_ids_do_not_meet_upgrade_threshold(self):
        registry = CapabilityRegistry(self.root / "tier-unique-evidence")
        active = camera_inventory_quick_bundle()
        active["selector"]["tier"] = "openclaw_only"
        registry.install_initial(active)
        template = {
            "capability": "camera_inventory_quick", "capability_version": 1,
            "success": True, "selector_tier": "openclaw_only",
            "risk_class": "read_only", "execution_class": "quick_tool",
            "tool_trace": [{
                "provider": "miloco", "tool": "miloco.device_list", "status": "completed",
            }],
        }
        repeated = [{**template, "turn_id": "same-turn", "time": "2026-07-29T08:00:00+08:00", "request": f"问法{index}"} for index in range(5)]
        repeated += [{**template, "turn_id": "", "time": "2026-07-30T08:00:00+08:00", "request": f"另一问法{index}"} for index in range(5)]
        self.assertEqual(propose_capability_tier_changes(repeated, registry), [])
        unique = [{
            **template, "turn_id": f"unique-{index}",
            "time": f"2026-07-{29 + (index % 2):02d}T08:00:00+08:00",
            "request": ["家里有几台摄像头", "有哪些摄像头", "摄像头清单"][index % 3],
        } for index in range(5)]
        self.assertEqual(len(propose_capability_tier_changes(unique, registry)), 1)

    def test_tier_upgrade_requires_cross_day_and_expression_diversity(self):
        registry = CapabilityRegistry(self.root / "tier-diversity")
        active = camera_inventory_quick_bundle()
        active["selector"]["tier"] = "openclaw_only"
        registry.install_initial(active)
        template = {
            "capability": "camera_inventory_quick", "capability_version": 1,
            "success": True, "selector_tier": "openclaw_only",
            "risk_class": "read_only", "execution_class": "quick_tool",
            "tool_trace": [{"provider": "miloco", "tool": "miloco.device_list", "status": "completed"}],
        }
        one_day = [{**template, "turn_id": f"d-{i}", "time": "2026-07-29T08:00:00+08:00", "request": f"问法{i}"} for i in range(5)]
        self.assertEqual(propose_capability_tier_changes(one_day, registry), [])
        two_expressions = [{
            **template, "turn_id": f"e-{i}",
            "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": ["2026年07月29日 08:00 示例用户: 查摄像头", "摄像头有哪些"][i % 2],
        } for i in range(5)]
        self.assertEqual(propose_capability_tier_changes(two_expressions, registry), [])

    def test_4b_to_08b_needs_ten_turns_three_days_and_five_expressions(self):
        registry = CapabilityRegistry(self.root / "tier-08b")
        active = camera_inventory_quick_bundle()
        active["selector"]["tier"] = "4b_eligible"
        registry.install_initial(active)
        turns = [{
            "turn_id": f"four-b-{i}", "time": f"2026-07-{28 + i % 3:02d}T08:00:00+08:00",
            "request": ["查摄像头", "摄像头清单", "几个摄像头", "有哪些摄像头", "列出摄像头"][i % 5],
            "capability": "camera_inventory_quick", "capability_version": 1,
            "success": True, "selector_tier": "4b_eligible", "risk_class": "read_only",
            "execution_class": "quick_tool",
            "tool_trace": [{"provider": "miloco", "tool": "miloco.device_list", "status": "completed"}],
        } for i in range(10)]
        proposals = propose_capability_tier_changes(turns, registry)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["target_tier"], "08b_eligible")

    def test_repeated_stable_openclaw_reads_create_review_only_quick_tool_draft(self):
        rows = []
        for index, request in enumerate(["查龟灯状态", "饲养灯开着吗", "查龟灯状态"]):
            rows.append({
                "turn_id": f"discover-{index}",
                "time": f"2026-07-{29 + index % 2:02d}T08:00:00+08:00",
                "request": request, "success": True,
                "execution_class": "openclaw", "risk_class": "read_only",
                "capability": "none", "discovery_key": "turtle_lamp_status",
                "tool_trace": [{
                    "provider": "miloco", "tool": "miloco.device_status", "status": "completed",
                }],
            })
        drafts = discover_quick_tool_drafts(rows, self.registry)
        self.assertEqual(len(drafts), 1)
        draft = drafts[0]
        self.assertEqual(draft["key"], "turtle_lamp_status")
        self.assertEqual(draft["status"], "needs_recipe_review")
        self.assertEqual(draft["initial_tier"], "openclaw_only")
        self.assertEqual(draft["observed_tool_signature"], ["miloco:miloco.device_status"])
        self.assertEqual(draft["evidence_ids"], ["discover-0", "discover-1", "discover-2"])
        self.assertEqual(set(draft["examples"]), {"查龟灯状态", "饲养灯开着吗"})
        self.assertNotIn("capability", draft)

    def test_observed_capability_candidate_rejects_turn_with_any_failed_trace(self):
        rows = [{
            "turn_id": "mixed-trace", "request": "有哪些摄像头", "success": True,
            "risk_class": "read_only",
            "level2": {"decision": "quick_tool", "quick_tool_id": "camera_inventory_quick"},
            "tool_trace": [
                {"provider": "miloco", "tool": "miloco.device_list", "status": "completed"},
                {"provider": "bridge", "tool": "postprocess", "status": "failed"},
            ],
        }]
        self.write_turns(rows)
        proposals = propose_observed_capabilities(
            self.feedback / "turns.jsonl", self.registry,
        )
        self.assertEqual(proposals, [])
        self.assertIsNone(self.registry.candidate("camera_inventory_quick"))

    def test_discovery_rejects_generic_tools_missing_keys_and_failed_or_mutating_turns(self):
        common = {
            "time": "2026-07-29T08:00:00+08:00", "execution_class": "openclaw",
            "risk_class": "read_only", "success": True, "request": "测试",
        }
        rows = [
            {**common, "turn_id": "exec", "discovery_key": "unsafe_exec", "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}]},
            {**common, "turn_id": "missing-key", "tool_trace": [{"provider": "miloco", "tool": "miloco.device_status", "status": "completed"}]},
            {**common, "turn_id": "failed", "discovery_key": "failed_read", "success": False, "tool_trace": [{"provider": "miloco", "tool": "miloco.device_status", "status": "failed"}]},
            {**common, "turn_id": "write", "discovery_key": "device_control", "risk_class": "mutation", "tool_trace": [{"provider": "miloco", "tool": "miloco.device_action", "status": "completed"}]},
        ]
        self.assertEqual(discover_quick_tool_drafts(rows, self.registry), [])

    def test_daily_persists_discovery_drafts_privately_without_registry_candidate(self):
        rows = [{
            "turn_id": f"persist-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": ["查灯状态", "灯开着吗"][i % 2], "success": True,
            "execution_class": "openclaw", "risk_class": "read_only",
            "capability": "none", "discovery_key": "lamp_status",
            "tool_trace": [{"provider": "miloco", "tool": "miloco.device_status", "status": "completed"}],
        } for i in range(3)]
        self.write_turns(rows)
        result = run_daily_v2(self.feedback, self.registry, evaluator=lambda _: {})
        self.assertEqual(result["quick_tool_discovery_count"], 1)
        path = self.feedback / "quick-tool-discoveries.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        stored = json.loads(path.read_text())
        self.assertEqual(stored[0]["status"], "needs_recipe_review")
        self.assertIsNone(self.registry.active("lamp_status"))
        self.assertIsNone(self.registry.candidate("lamp_status"))

    def test_mimo_can_only_group_existing_turn_ids_into_review_only_drafts(self):
        rows = [{
            "turn_id": f"semantic-{i}",
            "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": ["查询饲养灯状态", "饲养灯开着吗"][i % 2],
            "success": True, "execution_class": "openclaw",
            "risk_class": "unrecorded", "capability": "none",
            "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        calls = []
        drafts = discover_openclaw_patterns(rows, self.registry, classifier=lambda prompt: calls.append(prompt) or json.dumps({
            "groups": [{
                "key": "turtle_lamp_status", "kind": "read_only_deterministic",
                "confidence": 0.98,
                "turn_ids": ["semantic-0", "semantic-1", "semantic-2"],
            }],
        }))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("answer", calls[0])
        self.assertNotIn("arguments", calls[0])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["status"], "needs_recipe_review")
        self.assertFalse(drafts[0]["recipe_eligible"])
        self.assertEqual(drafts[0]["observed_tool_signature"], ["openclaw:exec"])

    def test_discovery_parser_rejects_invented_ids_low_confidence_and_action_groups(self):
        allowed = {"t1", "t2", "t3"}
        bad = [
            {"groups": [{"key": "invented", "kind": "read_only_deterministic", "confidence": 0.99, "turn_ids": ["t1", "t2", "made-up"]}]},
            {"groups": [{"key": "uncertain", "kind": "read_only_deterministic", "confidence": 0.94, "turn_ids": ["t1", "t2", "t3"]}]},
            {"groups": [{"key": "control", "kind": "action", "confidence": 0.99, "turn_ids": ["t1", "t2", "t3"]}]},
        ]
        for value in bad:
            with self.assertRaises(ValueError):
                parse_discovery_groups(json.dumps(value), allowed)

    def test_semantic_discovery_cache_skips_unchanged_daily_provider_call(self):
        rows = [{
            "turn_id": f"cache-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": ["查灯状态", "灯开着吗"][i % 2], "success": True,
            "execution_class": "openclaw", "risk_class": "unrecorded",
            "capability": "none", "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        self.write_turns(rows)
        calls = []
        classifier = lambda _prompt: calls.append(True) or json.dumps({"groups": [{
            "key": "lamp_status", "kind": "read_only_deterministic", "confidence": 0.99,
            "turn_ids": ["cache-0", "cache-1", "cache-2"],
        }]})
        first = run_daily_v2(self.feedback, self.registry, evaluator=lambda _: {}, discovery_classifier=classifier)
        second = run_daily_v2(self.feedback, self.registry, evaluator=lambda _: {}, discovery_classifier=classifier)
        self.assertEqual(first["quick_tool_discovery_count"], 1)
        self.assertEqual(second["quick_tool_discovery_count"], 1)
        self.assertEqual(calls, [True])

    def test_semantic_group_matching_existing_capability_does_not_create_new_draft(self):
        rows = [{
            "turn_id": f"existing-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": ["家里有几台摄像头", "摄像头有哪些"][i % 2], "success": True,
            "execution_class": "openclaw", "risk_class": "unrecorded", "capability": "none",
            "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        drafts = discover_openclaw_patterns(rows, self.registry, classifier=lambda _prompt: json.dumps({"groups": [{
            "key": "new_camera_query", "kind": "read_only_deterministic", "confidence": 0.99,
            "turn_ids": ["existing-0", "existing-1", "existing-2"],
        }]}))
        self.assertEqual(drafts, [])

    def test_new_similar_group_merges_into_existing_draft_key(self):
        rows = [{
            "turn_id": f"merge-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": ["查询饲养灯状态", "饲养灯开着吗", "看看龟缸灯现在亮没亮"][i],
            "success": True, "execution_class": "openclaw", "risk_class": "unrecorded",
            "capability": "none", "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        existing = [{
            "schema_version": 1, "key": "turtle_lamp_status",
            "status": "needs_recipe_review", "initial_tier": "openclaw_only",
            "recipe_eligible": False, "observed_tool_signature": ["openclaw:exec"],
            "examples": ["查询饲养灯状态", "饲养灯开着吗"],
            "evidence_ids": ["old-0", "old-1", "old-2"],
            "days": ["2026-07-29", "2026-07-30"], "confidence": 0.98,
        }]
        drafts = discover_openclaw_patterns(
            rows, self.registry, existing_drafts=existing,
            classifier=lambda prompt: json.dumps({"groups": [{
                "key": "turtle_lamp_status", "kind": "read_only_deterministic",
                "confidence": 0.99,
                "turn_ids": ["merge-0", "merge-1", "merge-2"],
            }]}),
        )
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["key"], "turtle_lamp_status")
        self.assertEqual(
            drafts[0]["evidence_ids"],
            ["old-0", "old-1", "old-2", "merge-0", "merge-1", "merge-2"],
        )
        self.assertIn("看看龟缸灯现在亮没亮", drafts[0]["examples"])

    def test_existing_draft_directory_rejects_unknown_reused_key(self):
        rows = [{
            "turn_id": f"unknown-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": f"问法{i}", "success": True, "execution_class": "openclaw",
            "risk_class": "unrecorded", "capability": "none",
            "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        with self.assertRaises(ValueError):
            discover_openclaw_patterns(
                rows, self.registry, existing_drafts=[{
                    "key": "known_draft", "status": "needs_recipe_review",
                    "examples": ["旧问法"], "observed_tool_signature": ["openclaw:exec"],
                }],
                classifier=lambda _prompt: json.dumps({"groups": [{
                    "key": "pretend_existing", "kind": "read_only_deterministic",
                    "confidence": 0.99, "reuse_existing": True,
                    "turn_ids": ["unknown-0", "unknown-1", "unknown-2"],
                }]}),
            )

    def test_empty_refresh_clears_stale_discovery_file(self):
        self.feedback.mkdir(parents=True, exist_ok=True)
        stale = self.feedback / "quick-tool-discoveries.json"
        stale.write_text('[{"key":"stale"}]\n')
        result = run_daily_v2(self.feedback, self.registry, evaluator=lambda _: {})
        self.assertEqual(result["quick_tool_discovery_count"], 0)
        self.assertEqual(json.loads(stale.read_text()), [])

    def test_production_evaluator_uses_current_schema_and_counts_dangerous_negatives(self):
        calls = []
        answers = iter([
            {"decision": "chat", "quick_tool_id": "none", "handoff": "none", "confidence": 0.99},
            {"decision": "handoff", "quick_tool_id": "none", "handoff": "lookup", "confidence": 0.99},
            {"decision": "handoff", "quick_tool_id": "none", "handoff": "action", "confidence": 0.99},
        ])

        def invoke(payload):
            calls.append(payload)
            return next(answers), 0.1

        metrics = ollama_level1_evaluator(
            [{"request": "打开灯", "decision": "handoff", "quick_tool_id": "none", "handoff": "action", "evidence_ids": ["t1"]}],
            registry=self.registry,
            baseline=[
                ("讲个笑话", "chat", "none", "none", False),
                ("今天北京天气", "handoff", "none", "lookup", True),
            ],
            invoke=invoke,
        )
        self.assertEqual(metrics["schema_rate"], 1.0)
        self.assertEqual(metrics["accuracy"], 1.0)
        self.assertEqual(metrics["dangerous_negative_accuracy"], 1.0)
        self.assertTrue(all(call["think"] is False for call in calls))
        self.assertTrue(all(call["format"]["additionalProperties"] is False for call in calls))

    def test_candidate_is_examined_but_not_injected_into_exam_system_prompt(self):
        calls = []
        candidate = {
            "request": "候选唯一原句XYZ", "decision": "chat",
            "quick_tool_id": "none", "handoff": "none", "evidence_ids": ["t1"],
        }
        ollama_level1_evaluator(
            [candidate], registry=self.registry, baseline=[],
            invoke=lambda payload: calls.append(payload) or ({
                "decision": "chat", "quick_tool_id": "none",
                "handoff": "none", "confidence": 0.99,
            }, 0.1),
        )
        self.assertTrue(any(call["prompt"] == "候选唯一原句XYZ" for call in calls))
        self.assertTrue(all("候选唯一原句XYZ" not in call["system"] for call in calls))

    def test_evaluator_failure_is_fail_closed(self):
        metrics = ollama_level1_evaluator(
            [], registry=self.registry,
            baseline=[("讲个笑话", "chat", "none", "none", False)],
            invoke=lambda _: (_ for _ in ()).throw(OSError("offline")),
        )
        self.assertEqual(metrics["schema_rate"], 0.0)
        self.assertEqual(metrics["accuracy"], 0.0)

    def test_second_daily_process_cannot_take_same_lock(self):
        self.feedback.mkdir(parents=True, exist_ok=True)
        with evolution_lock(self.feedback):
            with self.assertRaises(RuntimeError):
                with evolution_lock(self.feedback):
                    pass

    def test_daily_without_candidates_does_not_call_evaluator(self):
        called = []
        result = run_daily_v2(
            self.feedback, self.registry,
            evaluator=lambda _: called.append(True) or {},
        )
        self.assertEqual(result["level1"]["status"], "skipped")
        self.assertEqual(called, [])

    def test_production_daily_uses_lock_and_real_evaluator_contract(self):
        result = run_production_daily(
            self.feedback, self.registry,
            evaluator_factory=lambda registry: lambda examples: {
                "schema_rate": 1.0, "accuracy": 1.0,
                "dangerous_negative_accuracy": 1.0, "p95_seconds": 0.1,
            },
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue((self.feedback / ".evolution-v2.lock").exists())

    def test_discovery_classifier_failure_does_not_block_daily_evolution(self):
        rows = [{
            "turn_id": f"offline-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": ["查灯状态", "灯开着吗"][i % 2], "success": True,
            "execution_class": "openclaw", "risk_class": "unrecorded",
            "capability": "none", "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        self.write_turns(rows)
        result = run_daily_v2(
            self.feedback, self.registry, evaluator=lambda _: {},
            discovery_classifier=lambda _prompt: (_ for _ in ()).throw(OSError("offline")),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["quick_tool_discovery_count"], 0)
        self.assertEqual(result["quick_tool_discovery_status"], "classifier_failed")

    def test_classifier_failure_preserves_existing_discovery_drafts(self):
        self.feedback.mkdir(parents=True, exist_ok=True)
        existing = [{
            "schema_version": 1, "key": "turtle_lamp_status",
            "status": "needs_recipe_review", "initial_tier": "openclaw_only",
            "recipe_eligible": False, "observed_tool_signature": ["openclaw:exec"],
            "examples": ["查询饲养灯状态", "饲养灯开着吗"],
            "evidence_ids": ["old-1", "old-2", "old-3"],
            "days": ["2026-07-29", "2026-07-30"], "confidence": 0.98,
        }]
        (self.feedback / "quick-tool-discoveries.json").write_text(json.dumps(existing))
        self.write_turns([{
            "turn_id": f"failure-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": f"新问法{i}", "success": True, "execution_class": "openclaw",
            "risk_class": "unrecorded", "capability": "none",
            "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)])
        result = run_daily_v2(
            self.feedback, self.registry, evaluator=lambda _: {},
            discovery_classifier=lambda _prompt: (_ for _ in ()).throw(OSError("offline")),
        )
        self.assertEqual(result["quick_tool_discovery_status"], "classifier_failed")
        self.assertEqual(json.loads((self.feedback / "quick-tool-discoveries.json").read_text()), existing)

    def test_poisoned_cache_is_rejected_and_never_persisted_as_draft(self):
        self.feedback.mkdir(parents=True, exist_ok=True)
        self.write_turns([{
            "turn_id": f"poison-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": f"查状态{i}", "success": True, "execution_class": "openclaw",
            "risk_class": "unrecorded", "capability": "none",
            "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)])
        poison = [{"key": "poison", "status": "approved", "recipe_eligible": True,
                   "recipe": {"tool": "exec"}}]
        (self.feedback / "quick-tool-discovery-cache.json").write_text(json.dumps({
            "input_hash": "attacker-controlled", "drafts": poison,
        }))
        result = run_daily_v2(
            self.feedback, self.registry, evaluator=lambda _: {},
            discovery_classifier=lambda _prompt: '{"groups":[]}',
            discovery_date="2026-07-30",
        )
        self.assertNotEqual(result["quick_tool_discovery_status"], "cached")
        self.assertEqual(json.loads((self.feedback / "quick-tool-discoveries.json").read_text()), [])

    def test_mixed_tool_signatures_do_not_merge_into_existing_draft(self):
        existing = [{
            "schema_version": 1, "key": "lamp_status", "status": "needs_recipe_review",
            "initial_tier": "openclaw_only", "recipe_eligible": False,
            "observed_tool_signature": ["openclaw:exec"], "examples": ["查灯", "灯开着吗"],
            "evidence_ids": ["old-1", "old-2", "old-3"],
            "days": ["2026-07-28", "2026-07-29"], "confidence": 0.98,
        }]
        rows = [{
            "turn_id": f"mixed-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": f"灯状态问法{i}", "success": True, "execution_class": "openclaw",
            "risk_class": "unrecorded", "capability": "none",
            "tool_trace": [{"provider": "openclaw", "tool": ["exec", "other", "exec"][i], "status": "completed"}],
        } for i in range(3)]
        drafts = discover_openclaw_patterns(
            rows, self.registry, existing_drafts=existing,
            classifier=lambda _prompt: json.dumps({"groups": [{
                "key": "lamp_status", "kind": "read_only_deterministic", "confidence": 0.99,
                "turn_ids": ["mixed-0", "mixed-1", "mixed-2"],
            }]}),
        )
        self.assertEqual(drafts, [])

    def test_semantic_discovery_excludes_turn_with_any_failed_tool_trace(self):
        rows = [{
            "turn_id": f"trace-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": f"状态问法{i}", "success": True, "execution_class": "openclaw",
            "risk_class": "unrecorded", "capability": "none",
            "tool_trace": [
                {"provider": "openclaw", "tool": "exec", "status": "completed"},
                *([{"provider": "openclaw", "tool": "other", "status": "failed"}] if i == 2 else []),
            ],
        } for i in range(3)]
        calls = []
        drafts = discover_openclaw_patterns(
            rows, self.registry,
            classifier=lambda prompt: calls.append(prompt) or '{"groups":[]}',
        )
        self.assertEqual(drafts, [])
        self.assertEqual(calls, [])

    def test_cloud_prompt_redacts_credentials_and_common_personal_identifiers(self):
        rows = [{
            "turn_id": f"secret-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": f"查状态 password=SECRET{i} token: TOK{i} password PLAIN{i} token TOKPLAIN{i} 邮箱 a{i}@example.com 手机 1380013800{i} 或 +86 138-0013-8000",
            "success": True, "execution_class": "openclaw", "risk_class": "unrecorded",
            "capability": "none", "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        prompts = []
        existing = [{
            "schema_version": 1, "key": "private_status", "status": "needs_recipe_review",
            "initial_tier": "openclaw_only", "recipe_eligible": False,
            "observed_tool_signature": ["openclaw:exec"],
            "examples": ["credential=<REDACTED>", "状态查询"],
            "evidence_ids": ["old-a", "old-b", "old-c"],
            "days": ["2026-07-28", "2026-07-29"], "confidence": 0.99,
        }]
        discover_openclaw_patterns(
            rows, self.registry, existing_drafts=existing,
            classifier=lambda prompt: prompts.append(prompt) or '{"groups":[]}',
        )
        prompt = prompts[0]
        self.assertNotIn("SECRET", prompt)
        self.assertNotIn("TOK", prompt)
        self.assertNotIn("PLAIN", prompt)
        self.assertNotIn("@example.com", prompt)
        self.assertNotIn("1380013800", prompt)
        self.assertNotIn("138-0013-8000", prompt)
        self.assertIn("[REDACTED]", prompt)

    def test_daily_cloud_gate_allows_only_one_call_when_pool_changes(self):
        base = [{
            "turn_id": f"daily-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": f"状态问法{i}", "success": True, "execution_class": "openclaw",
            "risk_class": "unrecorded", "capability": "none",
            "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        self.write_turns(base)
        calls = []
        classifier = lambda _prompt: calls.append(True) or '{"groups":[]}'
        first = run_daily_v2(self.feedback, self.registry, evaluator=lambda _: {},
                             discovery_classifier=classifier, discovery_date="2026-07-30")
        self.write_turns(base + [{**base[0], "turn_id": "daily-3", "request": "第四种问法"}])
        second = run_daily_v2(self.feedback, self.registry, evaluator=lambda _: {},
                              discovery_classifier=classifier, discovery_date="2026-07-30")
        self.assertEqual(calls, [True])
        self.assertEqual(first["quick_tool_discovery_status"], "classified")
        self.assertEqual(second["quick_tool_discovery_status"], "daily_limit")

    def test_new_key_with_existing_signature_is_quarantined_not_duplicated(self):
        existing = [{
            "schema_version": 1, "key": "turtle_lamp_status", "status": "needs_recipe_review",
            "initial_tier": "openclaw_only", "recipe_eligible": False,
            "observed_tool_signature": ["openclaw:exec"],
            "examples": ["查询饲养灯状态", "饲养灯开着吗"],
            "evidence_ids": ["old-1", "old-2", "old-3"],
            "days": ["2026-07-28", "2026-07-29"], "confidence": 0.98,
        }]
        rows = [{
            "turn_id": f"alias-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": ["看看龟缸灯亮没亮", "确认龟灯状态", "龟缸的灯开着吗"][i],
            "success": True, "execution_class": "openclaw", "risk_class": "unrecorded",
            "capability": "none", "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        drafts, conflicts = discover_openclaw_patterns(
            rows, self.registry, existing_drafts=existing, return_conflicts=True,
            classifier=lambda _prompt: json.dumps({"groups": [{
                "key": "tank_light_query", "kind": "read_only_deterministic", "confidence": 0.99,
                "turn_ids": ["alias-0", "alias-1", "alias-2"],
            }]}),
        )
        self.assertEqual([item["key"] for item in drafts], ["tank_light_query"])
        self.assertEqual(conflicts, [])

    def test_similar_expression_with_different_signature_is_quarantined(self):
        existing = [{
            "schema_version": 1, "key": "turtle_lamp_status", "status": "needs_recipe_review",
            "initial_tier": "openclaw_only", "recipe_eligible": False,
            "observed_tool_signature": ["miloco:device_status"],
            "examples": ["查询饲养灯状态", "饲养灯开着吗"],
            "evidence_ids": ["old-1", "old-2", "old-3"],
            "days": ["2026-07-28", "2026-07-29"], "confidence": 0.98,
        }]
        rows = [{
            "turn_id": f"lex-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": ["看看饲养灯状态", "饲养灯现在开着吗", "确认饲养灯状态"][i],
            "success": True, "execution_class": "openclaw", "risk_class": "unrecorded",
            "capability": "none", "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        drafts, conflicts = discover_openclaw_patterns(
            rows, self.registry, existing_drafts=existing, return_conflicts=True,
            classifier=lambda _prompt: json.dumps({"groups": [{
                "key": "tank_light_query", "kind": "read_only_deterministic", "confidence": 0.99,
                "turn_ids": ["lex-0", "lex-1", "lex-2"],
            }]}),
        )
        self.assertEqual([item["key"] for item in drafts], ["tank_light_query"])
        self.assertEqual(conflicts, [])

    def test_semantic_synonym_new_key_is_blocked_by_existing_catalog(self):
        existing = [{
            "schema_version": 1, "key": "camera_inventory", "status": "needs_recipe_review",
            "initial_tier": "openclaw_only", "recipe_eligible": False,
            "observed_tool_signature": ["miloco:device_list"],
            "examples": ["摄像头清单", "家里有哪些摄像头"],
            "evidence_ids": ["old-1", "old-2", "old-3"],
            "days": ["2026-07-28", "2026-07-29"], "confidence": 0.99,
        }]
        rows = [{
            "turn_id": f"syn-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": ["列出监控设备", "查看家庭监控器列表", "查一下家里的监控装置"][i],
            "success": True, "execution_class": "openclaw", "risk_class": "unrecorded",
            "capability": "none", "tool_trace": [{"provider": "other", "tool": "inventory", "status": "completed"}],
        } for i in range(3)]
        drafts, conflicts = discover_openclaw_patterns(
            rows, self.registry, existing_drafts=existing, return_conflicts=True,
            classifier=lambda _prompt: json.dumps({"groups": [{
                "key": "monitor_device_list", "kind": "read_only_deterministic", "confidence": 0.99,
                "turn_ids": ["syn-0", "syn-1", "syn-2"],
            }]}),
        )
        self.assertEqual([item["key"] for item in drafts], ["monitor_device_list"])
        self.assertEqual(conflicts, [])

    def test_empty_catalog_multiple_new_keys_are_all_quarantined(self):
        rows = []
        for key, tool, prefix in (("camera_query", "camera_list", "c"), ("lamp_query", "lamp_status", "l")):
            for i in range(3):
                rows.append({
                    "turn_id": f"{prefix}-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
                    "request": f"{key}问法{i}", "success": True, "execution_class": "openclaw",
                    "risk_class": "unrecorded", "capability": "none",
                    "tool_trace": [{"provider": "other", "tool": tool, "status": "completed"}],
                })
        drafts, conflicts = discover_openclaw_patterns(
            rows, self.registry, return_conflicts=True,
            classifier=lambda _prompt: json.dumps({"groups": [
                {"key": "camera_query", "kind": "read_only_deterministic", "confidence": 0.99,
                 "turn_ids": ["c-0", "c-1", "c-2"]},
                {"key": "lamp_query", "kind": "read_only_deterministic", "confidence": 0.99,
                 "turn_ids": ["l-0", "l-1", "l-2"]},
            ]}),
        )
        self.assertEqual({item["key"] for item in drafts}, {"camera_query", "lamp_query"})
        self.assertEqual(conflicts, [])

    def test_mixed_invalid_state_preserves_valid_bindings_tombstones_and_drafts(self):
        from daily_evolution_v2 import _validated_bindings, _validated_drafts, _validated_tombstones
        valid_draft = {
            "schema_version": 1, "key": "canonical_old", "status": "needs_recipe_review",
            "initial_tier": "openclaw_only", "recipe_eligible": False,
            "observed_tool_signature": ["openclaw:exec"],
            "examples": ["状态查询", "查询状态"], "evidence_ids": ["old-1", "old-2", "old-3"],
            "days": ["2026-07-28", "2026-07-29"], "confidence": 0.99,
        }
        self.assertEqual(
            _validated_bindings({"bound-0": "canonical_old", "evil": "BAD-KEY"}),
            {"bound-0": "canonical_old"},
        )
        self.assertEqual(
            _validated_tombstones(["retired_query", "BAD-KEY"]), {"retired_query"},
        )
        self.assertEqual(
            _validated_drafts([valid_draft, {"key": "poison"}]), [valid_draft],
        )

    def test_deterministic_discovery_keys_with_same_signature_do_not_split(self):
        rows = []
        for key, prefix in (("alpha_key", "a"), ("beta_key", "b")):
            for i in range(3):
                rows.append({
                    "turn_id": f"{prefix}-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
                    "request": f"{key}问法{i}", "success": True, "execution_class": "openclaw",
                    "risk_class": "read_only", "capability": "none", "discovery_key": key,
                    "tool_trace": [{"provider": "miloco", "tool": "miloco.device_status", "status": "completed"}],
                })
        self.write_turns(rows)
        result = run_daily_v2(self.feedback, self.registry, evaluator=lambda _: {})
        self.assertEqual(result["quick_tool_discovery_count"], 0)
        conflicts = json.loads((self.feedback / "quick-tool-discovery-conflicts.json").read_text())
        self.assertEqual({item["reason"] for item in conflicts}, {"same_signature_multiple_new_keys"})

    def test_deterministic_multiple_new_keys_with_different_signatures_require_review(self):
        rows = []
        for key, tool, prefix in (("camera_key", "camera_list", "c"), ("lamp_key", "lamp_status", "l")):
            for i in range(3):
                rows.append({
                    "turn_id": f"det-{prefix}-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
                    "request": f"{key}问法{i}", "success": True, "execution_class": "openclaw",
                    "risk_class": "read_only", "capability": "none", "discovery_key": key,
                    "tool_trace": [{"provider": "miloco", "tool": tool, "status": "completed"}],
                })
        self.write_turns(rows)
        result = run_daily_v2(self.feedback, self.registry, evaluator=lambda _: {})
        self.assertEqual(result["quick_tool_discovery_count"], 0)
        conflicts = json.loads((self.feedback / "quick-tool-discovery-conflicts.json").read_text())
        self.assertEqual({item["reason"] for item in conflicts}, {"multiple_new_keys_require_review"})

    def test_cross_path_multiple_new_keys_are_all_quarantined(self):
        rows = []
        for i in range(3):
            rows.append({
                "turn_id": f"det-cross-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
                "request": f"确定性问法{i}", "success": True, "execution_class": "openclaw",
                "risk_class": "read_only", "capability": "none", "discovery_key": "det_key",
                "tool_trace": [{"provider": "miloco", "tool": "det_status", "status": "completed"}],
            })
            rows.append({
                "turn_id": f"sem-cross-{i}", "time": f"2026-07-{29 + i % 2:02d}T09:00:00+08:00",
                "request": f"语义问法{i}", "success": True, "execution_class": "openclaw",
                "risk_class": "unrecorded", "capability": "none",
                "tool_trace": [{"provider": "other", "tool": "semantic_status", "status": "completed"}],
            })
        self.write_turns(rows)
        result = run_daily_v2(
            self.feedback, self.registry, evaluator=lambda _: {}, discovery_date="2026-07-30",
            discovery_classifier=lambda _prompt: json.dumps({"groups": [{
                "key": "semantic_key", "kind": "read_only_deterministic", "confidence": 0.99,
                "turn_ids": ["sem-cross-0", "sem-cross-1", "sem-cross-2"],
            }]}),
        )
        self.assertEqual(result["quick_tool_discovery_count"], 1)
        drafts = json.loads((self.feedback / "quick-tool-discoveries.json").read_text())
        self.assertEqual([item["key"] for item in drafts], ["semantic_key"])

    def test_deterministic_alias_with_existing_signature_is_quarantined(self):
        existing = [{
            "schema_version": 1, "key": "canonical_status", "status": "needs_recipe_review",
            "initial_tier": "openclaw_only", "recipe_eligible": False,
            "observed_tool_signature": ["miloco:miloco.device_status"],
            "examples": ["查询状态", "确认状态"], "evidence_ids": ["old-1", "old-2", "old-3"],
            "days": ["2026-07-28", "2026-07-29"], "confidence": 1.0,
        }]
        self.feedback.mkdir(parents=True, exist_ok=True)
        (self.feedback / "quick-tool-discoveries.json").write_text(json.dumps(existing))
        rows = [{
            "turn_id": f"alias-det-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": f"别名状态问法{i}", "success": True, "execution_class": "openclaw",
            "risk_class": "read_only", "capability": "none", "discovery_key": "alias_key",
            "tool_trace": [{"provider": "miloco", "tool": "miloco.device_status", "status": "completed"}],
        } for i in range(3)]
        self.write_turns(rows)
        result = run_daily_v2(self.feedback, self.registry, evaluator=lambda _: {})
        self.assertEqual(result["quick_tool_discovery_count"], 1)
        drafts = json.loads((self.feedback / "quick-tool-discoveries.json").read_text())
        self.assertEqual([item["key"] for item in drafts], ["canonical_status"])
        conflicts = json.loads((self.feedback / "quick-tool-discovery-conflicts.json").read_text())
        self.assertEqual(conflicts[0]["reason"], "signature_matches_existing_draft")

    def test_turn_binding_prevents_reclassification_to_another_key(self):
        rows = [{
            "turn_id": f"bound-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": f"状态问法{i}", "success": True, "execution_class": "openclaw",
            "risk_class": "unrecorded", "capability": "none",
            "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        drafts, conflicts = discover_openclaw_patterns(
            rows, self.registry, turn_bindings={"bound-0": "canonical_old"}, return_conflicts=True,
            classifier=lambda _prompt: json.dumps({"groups": [{
                "key": "different_key", "kind": "read_only_deterministic", "confidence": 0.99,
                "turn_ids": ["bound-0", "bound-1", "bound-2"],
            }]}),
        )
        self.assertEqual(drafts, [])
        self.assertEqual(conflicts[0]["reason"], "turn_binding_conflict")

    def test_tombstoned_key_cannot_be_recreated(self):
        rows = [{
            "turn_id": f"dead-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": f"旧需求{i}", "success": True, "execution_class": "openclaw",
            "risk_class": "unrecorded", "capability": "none",
            "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        drafts, conflicts = discover_openclaw_patterns(
            rows, self.registry, tombstones={"retired_query"}, return_conflicts=True,
            classifier=lambda _prompt: json.dumps({"groups": [{
                "key": "retired_query", "kind": "read_only_deterministic", "confidence": 0.99,
                "turn_ids": ["dead-0", "dead-1", "dead-2"],
            }]}),
        )
        self.assertEqual(drafts, [])
        self.assertEqual(conflicts[0]["reason"], "tombstoned_key")

    def test_daily_persists_canonical_binding_and_quarantines_later_alias_key(self):
        first_rows = [{
            "turn_id": f"canon-{i}", "time": f"2026-07-{29 + i % 2:02d}T08:00:00+08:00",
            "request": ["查饲养灯", "饲养灯开着吗", "确认龟灯状态"][i],
            "success": True, "execution_class": "openclaw", "risk_class": "unrecorded",
            "capability": "none", "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        self.write_turns(first_rows)
        first = run_daily_v2(
            self.feedback, self.registry, evaluator=lambda _: {}, discovery_date="2026-07-30",
            discovery_classifier=lambda _prompt: json.dumps({"groups": [{
                "key": "turtle_lamp_status", "kind": "read_only_deterministic", "confidence": 0.99,
                "turn_ids": ["canon-0", "canon-1", "canon-2"],
            }]}),
        )
        self.assertEqual(first["quick_tool_discovery_count"], 1)
        bindings = json.loads((self.feedback / "quick-tool-turn-bindings.json").read_text())
        self.assertEqual(set(bindings.values()), {"turtle_lamp_status"})

        later = first_rows + [{
            "turn_id": f"later-{i}", "time": f"2026-07-{30 + i % 2:02d}T09:00:00+08:00",
            "request": ["看看龟缸灯亮没亮", "龟缸灯状态", "灯现在亮吗"][i],
            "success": True, "execution_class": "openclaw", "risk_class": "unrecorded",
            "capability": "none", "tool_trace": [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        } for i in range(3)]
        self.write_turns(later)
        second = run_daily_v2(
            self.feedback, self.registry, evaluator=lambda _: {}, discovery_date="2026-07-31",
            discovery_classifier=lambda _prompt: json.dumps({"groups": [{
                "key": "tank_light_query", "kind": "read_only_deterministic", "confidence": 0.99,
                "turn_ids": ["later-0", "later-1", "later-2"],
            }]}),
        )
        self.assertEqual(second["quick_tool_discovery_count"], 2)
        drafts = json.loads((self.feedback / "quick-tool-discoveries.json").read_text())
        self.assertEqual(
            {item["key"] for item in drafts}, {"turtle_lamp_status", "tank_light_query"},
        )

    def test_deterministic_same_key_merges_new_evidence_without_classifier(self):
        existing = [{
            "schema_version": 1, "key": "canonical_status", "status": "needs_recipe_review",
            "initial_tier": "openclaw_only", "recipe_eligible": False,
            "observed_tool_signature": ["miloco:miloco.device_status"],
            "examples": ["旧问法一", "旧问法二"],
            "evidence_ids": ["old-1", "old-2", "old-3"],
            "days": ["2026-07-28", "2026-07-29"], "confidence": 0.99,
        }]
        self.feedback.mkdir(parents=True, exist_ok=True)
        (self.feedback / "quick-tool-discoveries.json").write_text(json.dumps(existing))
        rows = [{
            "turn_id": f"merge-det-{i}", "time": f"2026-07-{30 + i % 2:02d}T08:00:00+08:00",
            "request": f"新问法{i}", "success": True, "execution_class": "openclaw",
            "risk_class": "read_only", "capability": "none", "discovery_key": "canonical_status",
            "tool_trace": [{"provider": "miloco", "tool": "miloco.device_status", "status": "completed"}],
        } for i in range(3)]
        self.write_turns(rows)
        result = run_daily_v2(self.feedback, self.registry, evaluator=lambda _: {})
        self.assertEqual(result["quick_tool_discovery_count"], 1)
        draft = json.loads((self.feedback / "quick-tool-discoveries.json").read_text())[0]
        self.assertEqual(draft["key"], "canonical_status")
        self.assertTrue({"old-1", "old-2", "old-3", "merge-det-0", "merge-det-1", "merge-det-2"}.issubset(draft["evidence_ids"]))
        self.assertIn("新问法0", draft["examples"])
        self.assertIn("2026-07-30", draft["days"])


if __name__ == "__main__":
    unittest.main()
