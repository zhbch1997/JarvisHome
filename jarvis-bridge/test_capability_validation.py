import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from capability_defaults import camera_inventory_bundle
from capability_registry import CapabilityRegistry, CapabilityValidationError
from capability_validation import CapabilityValidator, canonical_hash


class FakeRuntime:
    def __init__(self, result="家里一共3台摄像头。"):
        self.result = result
        self.calls = []

    def execute(self, bundle):
        self.calls.append(bundle["version"])
        return self.result


class CapabilityValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = CapabilityRegistry(self.root / "registry")
        active = camera_inventory_bundle()
        self.registry.install_initial(active)
        candidate = json.loads(json.dumps(active))
        candidate["version"] = 2
        candidate["status"] = "proposed"
        candidate["routing"]["examples"].append("列出家里的监控设备")
        self.registry.propose(candidate, ["turn-1"])

    def test_validator_persists_hash_bound_real_gate_report(self):
        runtime = FakeRuntime()
        validator = CapabilityValidator(
            self.root / "reports", self.registry, runtime,
            replay_runner=lambda bundle: {
                "passed": True, "sample_count": len(bundle["routing"]["examples"]),
                "accuracy": 1.0, "valid_json_rate": 1.0,
            },
        )

        report = validator.validate_candidate("camera_inventory")

        proposal = self.registry.candidate("camera_inventory")
        active = self.registry.active("camera_inventory")
        self.assertEqual(report["candidate_hash"], canonical_hash(proposal["capability"]))
        self.assertEqual(report["active_hash"], canonical_hash(active))
        self.assertEqual(report["active_version"], 1)
        self.assertTrue(report["passed"])
        self.assertTrue(report["gates"]["replay"]["passed"])
        self.assertTrue(report["gates"]["shadow"]["passed"])
        self.assertTrue(report["gates"]["security"]["passed"])
        self.assertEqual(runtime.calls, [1, 2])
        saved = validator.report(report["report_hash"])
        self.assertEqual(saved, report)
        self.assertEqual(validator.report_path(report["report_hash"]).stat().st_mode & 0o777, 0o600)

    def test_failed_shadow_report_is_persisted_but_cannot_promote(self):
        class DifferentRuntime:
            def execute(self, bundle):
                return f"version-{bundle['version']}"

        validator = CapabilityValidator(
            self.root / "reports", self.registry, DifferentRuntime(),
            replay_runner=lambda bundle: {
                "passed": True, "sample_count": 1,
                "accuracy": 1.0, "valid_json_rate": 1.0,
            },
        )
        report = validator.validate_candidate("camera_inventory")

        self.assertFalse(report["passed"])
        self.assertFalse(report["gates"]["shadow"]["passed"])
        self.assertIsNotNone(validator.report(report["report_hash"]))
        with self.assertRaises(CapabilityValidationError):
            self.registry.promote_with_report(
                "camera_inventory", expected_version=1, report=report,
            )
        self.assertEqual(self.registry.active("camera_inventory")["version"], 1)

    def test_candidate_change_after_validation_is_rechecked_inside_commit(self):
        validator = CapabilityValidator(
            self.root / "reports", self.registry, FakeRuntime(),
            replay_runner=lambda bundle: {
                "passed": True, "sample_count": 1,
                "accuracy": 1.0, "valid_json_rate": 1.0,
            },
        )
        report = validator.validate_candidate("camera_inventory")
        replacement = camera_inventory_bundle()
        replacement["version"] = 2
        replacement["status"] = "proposed"
        replacement["routing"]["examples"].append("替换后的候选")
        candidate_path = self.registry._path(
            self.registry.candidate_root, "camera_inventory",
        )
        candidate_path.write_text(json.dumps({
            "status": "proposed", "capability": replacement,
            "from_version": 1, "evidence_ids": ["replacement"],
        }, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(CapabilityValidationError):
            self.registry.promote_with_report(
                "camera_inventory", expected_version=1, report=report,
            )
        self.assertEqual(self.registry.active("camera_inventory")["version"], 1)

    def test_registry_rejects_forged_or_stale_report(self):
        validator = CapabilityValidator(
            self.root / "reports", self.registry, FakeRuntime(),
            replay_runner=lambda bundle: {
                "passed": True, "sample_count": 1,
                "accuracy": 1.0, "valid_json_rate": 1.0,
            },
        )
        report = validator.validate_candidate("camera_inventory")

        with self.assertRaises(CapabilityValidationError):
            self.registry.promote_with_report(
                "camera_inventory", expected_version=1,
                report={**report, "candidate_hash": "0" * 64},
            )

        changed = self.registry.candidate("camera_inventory")
        changed["capability"]["routing"]["examples"].append("监控设备列表")
        path = self.registry._path(self.registry.candidate_root, "camera_inventory")
        path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(CapabilityValidationError):
            self.registry.promote_with_report(
                "camera_inventory", expected_version=1, report=report,
            )


    def test_passing_layer_specific_report_can_promote_one_tier_only(self):
        self.registry._path(
            self.registry.candidate_root, "camera_inventory",
        ).unlink(missing_ok=True)
        self.registry.propose_tier_change(
            "camera_inventory", "4b_eligible", ["long-term-evidence"],
        )
        observed_layers = []

        def dangerous(_bundle, layer):
            observed_layers.append(layer)
            return {"passed": True, "sample_count": 40, "accuracy": 1.0}

        validator = CapabilityValidator(
            self.root / "reports", self.registry, FakeRuntime(),
            replay_runner=lambda bundle: {
                "passed": True, "sample_count": 100,
                "accuracy": 1.0, "valid_json_rate": 1.0,
            },
            dangerous_negative_runner=dangerous,
        )
        report = validator.validate_candidate("camera_inventory")
        self.assertTrue(report["passed"])
        self.assertEqual(observed_layers, ["4b"])
        promoted = self.registry.promote_with_report(
            "camera_inventory", expected_version=1, report=report,
        )
        self.assertEqual(promoted["selector"]["tier"], "4b_eligible")
        self.assertEqual(
            self.registry.active("camera_inventory")["selector"]["tier"],
            "4b_eligible",
        )

    def test_safety_downgrade_requires_only_security_gate(self):
        root = self.root / "downgrade-registry"
        registry = CapabilityRegistry(root)
        active = camera_inventory_bundle()
        active["selector"] = {
            "tier": "08b_eligible", "description": "camera inventory",
            "positive_examples": ["家里有几台摄像头"],
            "dangerous_negatives": ["打开摄像头"],
            "min_confidence": {"0.8b": 0.99, "4b": 0.95},
        }
        active["taxonomy"] = {
            "domain_tags": ["home"],
            "execution_kind": "deterministic_query",
        }
        registry.install_initial(active)
        registry.propose_tier_change(
            "camera_inventory", "openclaw_only", ["safety-incident"],
        )
        replay_calls = []
        validator = CapabilityValidator(
            self.root / "downgrade-reports", registry, FakeRuntime(),
            replay_runner=lambda bundle: replay_calls.append(bundle) or {
                "passed": False,
            },
        )
        report = validator.validate_candidate("camera_inventory")
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["target_tier"], "openclaw_only")
        self.assertEqual(set(report["gates"]), {"security"})
        self.assertTrue(report["passed"])
        self.assertEqual(replay_calls, [])
        promoted = registry.promote_with_report(
            "camera_inventory", expected_version=1, report=report,
        )
        self.assertEqual(promoted["selector"]["tier"], "openclaw_only")

    def test_tier_upgrade_requires_layer_specific_replay_and_dangerous_negatives(self):
        self.registry._path(
            self.registry.candidate_root, "camera_inventory",
        ).unlink(missing_ok=True)
        self.registry.propose_tier_change(
            "camera_inventory", "4b_eligible", ["long-term-evidence"],
        )
        validator = CapabilityValidator(
            self.root / "reports", self.registry, FakeRuntime(),
            replay_runner=lambda bundle: {
                "passed": True, "sample_count": 100,
                "accuracy": 1.0, "valid_json_rate": 1.0,
            },
        )
        report = validator.validate_candidate("camera_inventory")
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["change_kind"], "selector_tier")
        self.assertEqual(report["from_tier"], "openclaw_only")
        self.assertEqual(report["target_tier"], "4b_eligible")
        self.assertEqual(set(report["gates"]), {
            "replay_4b", "dangerous_negatives_4b", "shadow", "security",
        })
        self.assertFalse(report["passed"])
        self.assertFalse(report["gates"]["dangerous_negatives_4b"]["passed"])
        with self.assertRaises(CapabilityValidationError):
            self.registry.promote_with_report(
                "camera_inventory", expected_version=1, report=report,
            )


if __name__ == "__main__":
    unittest.main()
