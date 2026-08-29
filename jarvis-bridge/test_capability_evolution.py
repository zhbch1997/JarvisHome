import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from capability_defaults import camera_inventory_bundle
from capability_evolution import propose_from_review
from capability_registry import CapabilityRegistry
from feedback_loop import RouteReview


class CapabilityEvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = CapabilityRegistry(Path(self.temp.name))
        self.registry.install_initial(camera_inventory_bundle())

    def test_review_creates_complete_next_version_bundle_not_partial_rule(self):
        turn = {"turn_id": "t1", "request": "监控设备都有哪些"}
        review = RouteReview(
            "home", "query", "external_home", 0.97, True,
            "应读取真实摄像头目录", "camera_inventory",
        )
        proposal = propose_from_review(self.registry, turn, review)
        bundle = proposal["capability"]
        self.assertEqual(bundle["version"], 2)
        self.assertIn("监控设备都有哪些", bundle["routing"]["examples"])
        self.assertEqual(bundle["execution"], camera_inventory_bundle()["execution"])
        self.assertEqual(bundle["permissions"], camera_inventory_bundle()["permissions"])
        self.assertEqual(bundle["delivery"], camera_inventory_bundle()["delivery"])
        self.assertEqual(proposal["evidence_ids"], ["t1"])
        self.assertEqual(self.registry.active("camera_inventory")["version"], 1)

    def test_review_never_raises_selector_tier_or_changes_execution_taxonomy(self):
        active = self.registry.active("camera_inventory")
        self.assertEqual(active["selector"]["tier"], "openclaw_only")
        forged = json.loads(json.dumps(active))
        forged["version"] = 2
        forged["status"] = "proposed"
        forged["selector"]["tier"] = "08b_eligible"
        forged["taxonomy"]["execution_kind"] = "composed_read"
        legacy_polluted_proposal = {
            "status": "proposed",
            "capability": forged,
            "from_version": active["version"],
            "evidence_ids": ["forged"],
        }
        polluted_path = self.registry._path(
            self.registry.candidate_root, "camera_inventory",
        )
        polluted_path.parent.mkdir(parents=True, exist_ok=True)
        polluted_path.write_text(
            json.dumps(legacy_polluted_proposal, ensure_ascii=False),
            encoding="utf-8",
        )

        review = RouteReview(
            "home", "query", "external_home", 0.99, True,
            "应读取真实摄像头目录", "camera_inventory",
        )
        proposal = propose_from_review(
            self.registry,
            {"turn_id": "review-safe", "request": "摄像头有哪些"},
            review,
        )
        candidate = proposal["capability"]
        self.assertEqual(candidate["selector"]["tier"], "openclaw_only")
        self.assertEqual(
            candidate["taxonomy"]["execution_kind"],
            active["taxonomy"]["execution_kind"],
        )
        self.assertEqual(candidate["execution"], active["execution"])
        self.assertEqual(candidate["permissions"], active["permissions"])

    def test_non_capability_review_does_not_create_bundle_candidate(self):
        review = RouteReview("local_chat", "chat", "local_9b", 0.95, True, "原路线正确")
        self.assertIsNone(propose_from_review(self.registry, {"turn_id": "t2", "request": "讲笑话"}, review))

    def test_repeated_review_merges_examples_and_evidence_into_same_next_version(self):
        review = RouteReview("home", "query", "external_home", 0.99, True, "目录查询", "camera_inventory")
        propose_from_review(self.registry, {"turn_id": "t1", "request": "监控设备有哪些"}, review)
        proposal = propose_from_review(self.registry, {"turn_id": "t2", "request": "列出家中摄像头"}, review)
        self.assertEqual(proposal["capability"]["version"], 2)
        self.assertIn("监控设备有哪些", proposal["capability"]["routing"]["examples"])
        self.assertIn("列出家中摄像头", proposal["capability"]["routing"]["examples"])
        self.assertEqual(proposal["evidence_ids"], ["t1", "t2"])


if __name__ == "__main__":
    unittest.main()
