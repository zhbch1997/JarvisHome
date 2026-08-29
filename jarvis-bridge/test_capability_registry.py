import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from capability_registry import CapabilityRegistry, CapabilityValidationError
from capability_defaults import home_device_action_bundle, home_scene_action_bundle


CAMERA = {
    "id": "camera_inventory", "version": 1, "status": "active",
    "routing": {
        "route": "home", "operation": "query",
        "examples": ["家里有几台摄像头", "有哪些摄像头"],
    },
    "execution": {
        "primary": "bridge_recipe", "producer": "miloco",
        "fallback": "openclaw", "recipe": "camera_inventory_v1",
    },
    "recipe": {
        "tool": "miloco.device_list",
        "transforms": [{"op": "filter_eq", "field": "category", "value": "camera"}],
        "response_template": "camera_inventory_zh",
    },
    "permissions": {
        "risk": "read_only", "allowed_tools": ["miloco.device_list"],
        "forbidden_tools": ["miloco.device_action", "shell", "web"],
    },
    "delivery": {
        "transition": "好的主人，我查一下。",
        "progress": "我正在读取设备目录。",
    },
    "review": {
        "minimum_samples": 20, "fact_consistency": 1.0,
        "tool_argument_accuracy": 1.0,
    },
}


class CapabilityRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = CapabilityRegistry(self.root)

    def test_valid_bundle_is_saved_privately_and_resolved_by_id(self):
        saved = self.registry.install_initial(CAMERA)
        self.assertEqual(saved["id"], "camera_inventory")
        self.assertEqual(self.registry.active("camera_inventory")["execution"]["producer"], "miloco")
        self.assertEqual((self.root / "active" / "camera_inventory.json").stat().st_mode & 0o777, 0o600)

    def test_device_action_bundle_is_a_registered_4b_write_capability(self):
        bundle = self.registry.install_initial(home_device_action_bundle())
        self.assertEqual(bundle["selector"]["tier"], "4b_eligible")
        self.assertEqual(bundle["execution"]["primary"], "local_4b_device")
        self.assertEqual(bundle["permissions"]["risk"], "device_action")
        self.assertEqual(bundle["recipe"]["tool"], "miloco.device_action")
        self.assertIn(
            "home_device_action",
            [item["id"] for item in self.registry.list_selectable("4b")],
        )

    def test_scene_action_bundle_is_the_only_scene_quick_tool(self):
        from capability_defaults import install_defaults
        install_defaults(self.registry)
        scene_tools = [
            item for item in self.registry.list_selectable("4b")
            if item["taxonomy"]["execution_kind"] == "scene_action"
        ]
        self.assertEqual([item["id"] for item in scene_tools], ["home_scene_action"])
        bundle = scene_tools[0]
        self.assertIn("该睡觉了", bundle["selector"]["positive_examples"])
        self.assertIn("空调睡眠24度", bundle["selector"]["positive_examples"])

    def test_legacy_bundle_defaults_to_openclaw_only_selector(self):
        validated = self.registry.validate(CAMERA)
        self.assertEqual(validated["selector"]["tier"], "openclaw_only")
        self.assertEqual(validated["taxonomy"]["domain_tags"], ["home"])
        self.assertEqual(validated["taxonomy"]["execution_kind"], "deterministic_query")

    def test_accepts_dynamic_domain_tags_without_business_enum(self):
        bundle = json.loads(json.dumps(CAMERA))
        bundle["selector"] = {
            "tier": "4b_eligible",
            "description": "搜索并比较已授权来源中的商品",
            "positive_examples": ["帮我找一款静音仓鼠跑轮"],
            "dangerous_negatives": ["直接付款买这个"],
            "min_confidence": {"0.8b": 0.99, "4b": 0.95},
        }
        bundle["taxonomy"] = {
            "domain_tags": ["shopping", "pet_care"],
            "execution_kind": "deterministic_query",
        }
        validated = self.registry.validate(bundle)
        self.assertEqual(validated["selector"]["tier"], "4b_eligible")
        self.assertEqual(validated["taxonomy"]["domain_tags"], ["shopping", "pet_care"])

    def test_rejects_invalid_selector_tier_execution_kind_and_domain_tag(self):
        for mutate in (
            lambda x: x["selector"].update(tier="auto_pay"),
            lambda x: x["taxonomy"].update(execution_kind="python"),
            lambda x: x["taxonomy"].update(domain_tags=["shopping", "../../secret"]),
        ):
            value = json.loads(json.dumps(CAMERA))
            value["selector"] = {
                "tier": "4b_eligible", "description": "查询摄像头目录",
                "positive_examples": ["有哪些摄像头"],
                "dangerous_negatives": ["现在打开摄像头"],
                "min_confidence": {"0.8b": 0.99, "4b": 0.95},
            }
            value["taxonomy"] = {
                "domain_tags": ["home", "camera"],
                "execution_kind": "deterministic_query",
            }
            mutate(value)
            with self.assertRaises(CapabilityValidationError):
                self.registry.validate(value)

    def test_malformed_legacy_nested_objects_raise_validation_error(self):
        for field in ("routing", "execution"):
            value = json.loads(json.dumps(CAMERA))
            value[field] = None
            with self.subTest(field=field):
                with self.assertRaises(CapabilityValidationError):
                    self.registry.validate(value)

    def test_rejects_taxonomy_execution_kind_that_conflicts_with_executor(self):
        value = json.loads(json.dumps(CAMERA))
        value["selector"] = {
            "tier": "4b_eligible", "description": "查询摄像头目录",
            "positive_examples": ["有哪些摄像头"], "dangerous_negatives": [],
            "min_confidence": {"0.8b": 0.99, "4b": 0.95},
        }
        value["taxonomy"] = {
            "domain_tags": ["home", "camera"],
            "execution_kind": "openclaw_tool",
        }
        with self.assertRaises(CapabilityValidationError):
            self.registry.validate(value)

    def test_general_proposal_cannot_change_selector_tier(self):
        self.registry.install_initial(CAMERA)
        active = self.registry.active("camera_inventory")
        candidate = json.loads(json.dumps(active))
        candidate["version"] = 2
        candidate["status"] = "proposed"
        candidate["selector"]["tier"] = "4b_eligible"
        with self.assertRaises(CapabilityValidationError):
            self.registry.propose(candidate, ["review-evidence"])

    def test_selector_tier_upgrade_is_explicit_and_cannot_skip_a_level(self):
        self.registry.install_initial(CAMERA)
        active = self.registry.active("camera_inventory")
        self.assertEqual(active["selector"]["tier"], "openclaw_only")
        with self.assertRaises(CapabilityValidationError):
            self.registry.propose_tier_change(
                "camera_inventory", "08b_eligible", ["long-term-evidence"],
            )
        proposal = self.registry.propose_tier_change(
            "camera_inventory", "4b_eligible", ["long-term-evidence"],
        )
        self.assertEqual(proposal["change_kind"], "selector_tier")
        self.assertEqual(proposal["from_tier"], "openclaw_only")
        self.assertEqual(proposal["target_tier"], "4b_eligible")
        self.assertEqual(proposal["capability"]["selector"]["tier"], "4b_eligible")
        self.assertEqual(proposal["evidence_ids"], ["long-term-evidence"])

    def test_selector_tier_downgrade_can_move_directly_to_openclaw_only(self):
        active = self.registry.validate(CAMERA)
        active["version"] = 2
        active["selector"]["tier"] = "08b_eligible"
        other = CapabilityRegistry(Path(self.temp.name) / "downgrade")
        other.install_initial(active)
        proposal = other.propose_tier_change(
            "camera_inventory", "openclaw_only", ["safety-incident"],
        )
        self.assertEqual(proposal["from_tier"], "08b_eligible")
        self.assertEqual(proposal["target_tier"], "openclaw_only")

    def test_list_selectable_is_registry_driven_and_tiered(self):
        for index, tier in enumerate(("08b_eligible", "4b_eligible", "openclaw_only"), start=1):
            bundle = json.loads(json.dumps(CAMERA))
            bundle["id"] = f"camera_inventory_{index}"
            bundle["execution"]["recipe"] = f"camera_inventory_{index}_v1"
            bundle["selector"] = {
                "tier": tier, "description": f"selector {tier}",
                "positive_examples": [f"样本{index}"], "dangerous_negatives": [],
                "min_confidence": {"0.8b": 0.99, "4b": 0.95},
            }
            bundle["taxonomy"] = {
                "domain_tags": ["camera"], "execution_kind": "deterministic_query",
            }
            self.registry.install_initial(bundle)

        self.assertEqual(
            [item["id"] for item in self.registry.list_selectable("0.8b")],
            ["camera_inventory_1"],
        )
        self.assertEqual(
            [item["id"] for item in self.registry.list_selectable("4b")],
            ["camera_inventory_1", "camera_inventory_2"],
        )
        with self.assertRaises(CapabilityValidationError):
            self.registry.list_selectable("9b")

    def test_rejects_unknown_tool_transform_and_multiple_primary_executors(self):
        for mutate in (
            lambda x: x["recipe"].update(tool="shell.exec"),
            lambda x: x["recipe"]["transforms"].append({"op": "python", "code": "pass"}),
            lambda x: x["execution"].update(primary=["bridge_recipe", "openclaw"]),
        ):
            value = json.loads(json.dumps(CAMERA))
            mutate(value)
            with self.assertRaises(CapabilityValidationError):
                self.registry.validate(value)

    def test_rejects_executor_producer_permission_and_transition_mismatch(self):
        invalid = json.loads(json.dumps(CAMERA))
        invalid["execution"]["primary"] = "local_9b_agent"
        invalid["recipe"]["tool"] = "miloco.device_action"
        invalid["permissions"]["allowed_tools"] = ["miloco.device_action"]
        with self.assertRaises(CapabilityValidationError):
            self.registry.validate(invalid)

        invalid = json.loads(json.dumps(CAMERA))
        invalid["execution"]["producer"] = "openclaw"
        with self.assertRaises(CapabilityValidationError):
            self.registry.validate(invalid)

    def test_candidate_promotion_is_atomic_and_keeps_rollback_version(self):
        self.registry.install_initial(CAMERA)
        candidate = json.loads(json.dumps(CAMERA))
        candidate["version"] = 2
        candidate["routing"]["examples"].append("摄像头清单")
        proposal = self.registry.propose(candidate, evidence_ids=["f1", "f2"])
        self.assertEqual(proposal["status"], "proposed")
        promoted = self.registry._promote_validated("camera_inventory", expected_version=1)
        self.assertEqual(promoted["version"], 2)
        self.assertEqual(self.registry.rollback("camera_inventory")["version"], 1)

    def test_existing_candidate_cannot_be_overwritten_by_general_or_tier_proposal(self):
        self.registry.install_initial(CAMERA)
        first = json.loads(json.dumps(CAMERA))
        first["version"] = 2
        first["routing"]["examples"].append("保留这个候选")
        original = self.registry.propose(first, evidence_ids=["first"])

        replacement = json.loads(json.dumps(CAMERA))
        replacement["version"] = 2
        replacement["routing"]["examples"].append("覆盖候选")
        with self.assertRaises(CapabilityValidationError):
            self.registry.propose(replacement, evidence_ids=["second"])
        with self.assertRaises(CapabilityValidationError):
            self.registry.propose_tier_change(
                "camera_inventory", "4b_eligible", ["tier-change"],
            )
        self.assertEqual(self.registry.candidate("camera_inventory"), original)

    def test_candidate_merge_requires_matching_observed_hash(self):
        self.registry.install_initial(CAMERA)
        first = json.loads(json.dumps(CAMERA))
        first["version"] = 2
        original = self.registry.propose(first, evidence_ids=["first"])
        replacement = json.loads(json.dumps(first))
        replacement["routing"]["examples"].append("安全合并")
        with self.assertRaises(CapabilityValidationError):
            self.registry.replace_candidate_if_unchanged(
                replacement, ["first", "second"], "0" * 64,
            )
        merged = self.registry.replace_candidate_if_unchanged(
            replacement, ["first", "second"],
            self.registry.candidate_hash("camera_inventory"),
        )
        self.assertIn("安全合并", merged["capability"]["routing"]["examples"])
        self.assertEqual(merged["evidence_ids"], ["first", "second"])

    def test_candidate_snapshot_and_hash_come_from_same_read(self):
        self.registry.install_initial(CAMERA)
        candidate = json.loads(json.dumps(CAMERA))
        candidate["version"] = 2
        original = self.registry.propose(candidate, ["first"])
        snapshot, digest = self.registry.candidate_snapshot("camera_inventory")
        self.assertEqual(snapshot, original)
        self.assertEqual(digest, self.registry.candidate_hash("camera_inventory"))
        snapshot["evidence_ids"].append("mutated-copy")
        self.assertEqual(self.registry.candidate("camera_inventory"), original)

    def test_safety_downgrade_supersedes_existing_general_candidate(self):
        active = self.registry.validate(CAMERA)
        active["selector"]["tier"] = "08b_eligible"
        self.registry.install_initial(active)
        candidate = json.loads(json.dumps(active))
        candidate["version"] = 2
        candidate["routing"]["examples"].append("普通候选")
        self.registry.propose(candidate, ["ordinary"])
        proposal = self.registry.propose_safety_downgrade(
            "camera_inventory", ["incident-1"],
        )
        self.assertEqual(proposal["change_kind"], "selector_tier")
        self.assertEqual(proposal["target_tier"], "openclaw_only")
        self.assertEqual(proposal["evidence_ids"], ["incident-1"])

    def test_client_boolean_promotion_entrypoint_does_not_exist(self):
        self.assertFalse(hasattr(self.registry, "promote"))

    def test_active_uses_immutable_snapshot_and_atomic_pointer(self):
        installed = self.registry.install_initial(CAMERA)
        snapshots = list((self.root / "versions" / "camera_inventory").glob("v1-*.json"))
        self.assertEqual(len(snapshots), 1)
        pointer = json.loads((self.root / "active" / "camera_inventory.json").read_text())
        self.assertEqual(set(pointer), {
            "schema_version", "id", "version", "bundle_hash", "snapshot", "history",
        })
        self.assertEqual(installed, json.loads(snapshots[0].read_text()))
        self.assertEqual(snapshots[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "active" / "camera_inventory.json").stat().st_mode & 0o777, 0o600)

        candidate = json.loads(json.dumps(CAMERA))
        candidate["version"] = 2
        self.registry.propose(candidate, evidence_ids=["f1"])
        promoted = self.registry._promote_validated("camera_inventory", 1)
        self.assertEqual(promoted["version"], 2)
        self.assertEqual(len(list((self.root / "versions" / "camera_inventory").glob("v2-*.json"))), 1)
        self.assertEqual(self.registry.rollback("camera_inventory")["version"], 1)

    def test_pointer_backed_legacy_snapshot_is_normalized_only_in_memory(self):
        digest = self.registry._snapshot_path(CAMERA).stem.split("-", 1)[1]
        snapshot = self.root / "versions" / "camera_inventory" / f"v1-{digest}.json"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(json.dumps(CAMERA, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        pointer = self.root / "active" / "camera_inventory.json"
        pointer.parent.mkdir(parents=True)
        pointer_value = {
            "schema_version": 1, "id": "camera_inventory", "version": 1,
            "bundle_hash": digest,
            "snapshot": str(snapshot.relative_to(self.root)), "history": [],
        }
        pointer.write_text(json.dumps(pointer_value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        before_snapshot = snapshot.read_bytes()
        before_pointer = pointer.read_bytes()
        before_files = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))

        loaded = CapabilityRegistry(self.root).active("camera_inventory")

        self.assertEqual(loaded["selector"]["tier"], "openclaw_only")
        self.assertEqual(loaded["taxonomy"]["execution_kind"], "deterministic_query")
        self.assertEqual(snapshot.read_bytes(), before_snapshot)
        self.assertEqual(pointer.read_bytes(), before_pointer)
        self.assertEqual(
            sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")),
            before_files,
        )

    def test_corrupt_active_pointer_fails_closed_without_scanning_snapshots(self):
        self.registry.install_initial(CAMERA)
        pointer = self.root / "active" / "camera_inventory.json"
        pointer.write_text('{"version": 999}', encoding="utf-8")
        with self.assertRaises(CapabilityValidationError):
            self.registry.active("camera_inventory")

    def test_orphan_snapshot_does_not_activate_after_crash(self):
        self.registry.install_initial(CAMERA)
        orphan = json.loads(json.dumps(CAMERA))
        orphan["version"] = 2
        orphan["status"] = "active"
        self.registry._write_snapshot(orphan)

        self.assertEqual(self.registry.active("camera_inventory")["version"], 1)
        self.assertEqual(len(list((self.root / "versions" / "camera_inventory").glob("v2-*.json"))), 1)

    def test_legacy_active_bundle_migrates_to_snapshot_pointer(self):
        legacy = self.root / "active" / "camera_inventory.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(CAMERA), encoding="utf-8")
        migrated = CapabilityRegistry(self.root).active("camera_inventory")
        self.assertEqual(migrated["version"], 1)
        pointer = json.loads(legacy.read_text())
        self.assertEqual(pointer["schema_version"], 1)
        self.assertEqual(len(list((self.root / "versions" / "camera_inventory").glob("v1-*.json"))), 1)

    def test_rejects_stale_promotion_and_fallback_cycles(self):
        self.registry.install_initial(CAMERA)
        candidate = json.loads(json.dumps(CAMERA)); candidate["version"] = 2
        self.registry.propose(candidate, evidence_ids=[])
        with self.assertRaises(CapabilityValidationError):
            self.registry._promote_validated("camera_inventory", expected_version=0)
        cycle = json.loads(json.dumps(CAMERA))
        cycle["execution"]["primary"] = "openclaw"
        cycle["execution"]["fallback"] = "openclaw"
        with self.assertRaises(CapabilityValidationError):
            self.registry.validate(cycle)


if __name__ == "__main__":
    unittest.main()
