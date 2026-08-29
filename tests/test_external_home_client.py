import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "jarvis-bridge"))

from external_home_client import ExternalHomeClient, ExternalHomeConfigError


class ExternalHomeClientTests(unittest.TestCase):
    def test_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ExternalHomeConfigError, "disabled"):
                ExternalHomeClient.from_env()

    def test_enabled_plugin_requires_explicit_base_url(self):
        with patch.dict(os.environ, {"JARVIS_EXTERNAL_HOME_ENABLED": "1"}, clear=True):
            with self.assertRaisesRegex(ExternalHomeConfigError, "base URL"):
                ExternalHomeClient.from_env()

    def test_rejects_credentials_embedded_in_url(self):
        with self.assertRaisesRegex(ExternalHomeConfigError, "credentials"):
            ExternalHomeClient("http://user:secret@127.0.0.1:9000")

    def test_plain_http_is_limited_to_loopback(self):
        with self.assertRaisesRegex(ExternalHomeConfigError, "HTTPS"):
            ExternalHomeClient("http://192.0.2.10:9000")
        client = ExternalHomeClient("http://127.0.0.1:9000")
        self.assertEqual("http://127.0.0.1:9000", client.base_url)

    def test_device_list_uses_fixed_relative_json_endpoint(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"devices": [{"id": "demo-1"}]}
        transport = Mock()
        transport.get.return_value = response
        client = ExternalHomeClient("https://home.example.test/api", transport=transport)

        self.assertEqual([{"id": "demo-1"}], client.list_devices())
        transport.get.assert_called_once_with(
            "https://home.example.test/api/v1/devices", timeout=10.0,
        )

    def test_non_list_device_response_fails_closed(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"devices": "not-a-list"}
        transport = Mock()
        transport.get.return_value = response
        client = ExternalHomeClient("https://home.example.test", transport=transport)
        with self.assertRaisesRegex(RuntimeError, "invalid device response"):
            client.list_devices()

    def test_device_and_scene_ids_are_encoded_as_single_path_segments(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"specs": []}
        transport = Mock()
        transport.get.return_value = response
        transport.post.return_value = response
        client = ExternalHomeClient("https://home.example.test", transport=transport)

        hostile = "../room/a\\b?x=1#frag%2f"
        client.device_specs(hostile)
        client.control_device(hostile, "on", True)
        client.trigger_scene(hostile)

        encoded = "..%2Froom%2Fa%5Cb%3Fx%3D1%23frag%252f"
        transport.get.assert_called_once_with(
            f"https://home.example.test/v1/devices/{encoded}/specs", timeout=10.0,
        )
        self.assertEqual(
            f"https://home.example.test/v1/devices/{encoded}/actions",
            transport.post.call_args_list[0].args[0],
        )
        self.assertEqual(
            f"https://home.example.test/v1/scenes/{encoded}/trigger",
            transport.post.call_args_list[1].args[0],
        )

    def test_empty_and_oversized_identifiers_are_rejected_before_request(self):
        transport = Mock()
        client = ExternalHomeClient("https://home.example.test", transport=transport)
        for invalid in ("", "x" * 513):
            with self.subTest(length=len(invalid)):
                with self.assertRaisesRegex(RuntimeError, "invalid external home identifier"):
                    client.device_specs(invalid)
                with self.assertRaisesRegex(RuntimeError, "invalid external home identifier"):
                    client.control_device(invalid, "on", True)
                with self.assertRaisesRegex(RuntimeError, "invalid external home identifier"):
                    client.trigger_scene(invalid)
        transport.get.assert_not_called()
        transport.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
