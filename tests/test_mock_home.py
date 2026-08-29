import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jarvis-bridge"))

from mock_home import MockHomeAdapter, run_demo


class MockHomeTests(unittest.TestCase):
    def test_device_catalog_contains_only_generic_safe_devices(self):
        adapter = MockHomeAdapter()
        listing = adapter.cli(["device", "list"])
        self.assertIn("demo-light-1|示例灯|示例房间|light|online", listing)
        self.assertNotIn("camera", listing)
        self.assertNotIn("lock", listing)

    def test_control_records_action_without_external_side_effect(self):
        adapter = MockHomeAdapter()
        result = adapter.cli(["device", "control", "demo-light-1", "on", "true"])
        self.assertEqual("ok", result)
        self.assertEqual([{"device_id": "demo-light-1", "property": "on", "value": True}], adapter.actions)

    def test_unknown_or_dangerous_command_fails_closed(self):
        adapter = MockHomeAdapter()
        with self.assertRaisesRegex(ValueError, "unsupported mock command"):
            adapter.cli(["device", "action", "demo-lock", "unlock"])

    def test_demo_returns_machine_readable_evidence(self):
        result = run_demo()
        self.assertEqual("ok", result["status"])
        self.assertEqual("mock", result["adapter"])
        self.assertEqual(1, len(result["actions"]))
        self.assertEqual(True, result["actions"][0]["value"])
        json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
