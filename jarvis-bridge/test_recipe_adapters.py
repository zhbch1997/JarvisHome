import unittest
from unittest.mock import Mock, patch

from external_home_client import ExternalHomeConfigError
from recipe_adapters import camera_inventory_zh, external_home_device_list, parse_external_home_device_list
from recipe_runtime import RecipeExecutionError


class RecipeAdapterTests(unittest.TestCase):
    def test_device_list_parser_returns_structured_rows_and_keeps_non_cameras(self):
        stdout = "\n".join([
            "did-1 | 宠物龟摄像头 | 客厅 | camera | online",
            "did-2 | 小米智能门铃3 | 门口 | video-doorbell | online",
            "did-3 | 示例摄像头A | 示例房间 | camera | offline",
        ])

        rows = parse_external_home_device_list(stdout)

        self.assertEqual(rows, [
            {"id": "did-1", "name": "宠物龟摄像头", "room": "客厅", "category": "camera", "online": True},
            {"id": "did-2", "name": "小米智能门铃3", "room": "门口", "category": "video-doorbell", "online": True},
            {"id": "did-3", "name": "示例摄像头A", "room": "示例房间", "category": "camera", "online": False},
        ])

    def test_camera_template_uses_only_already_filtered_rows(self):
        text = camera_inventory_zh([
            {"name": "宠物龟摄像头", "room": "客厅", "online": True},
            {"name": "示例摄像头A", "room": "示例房间", "online": False},
        ])
        self.assertEqual(text, "家里一共2台摄像头：宠物龟摄像头（客厅，在线）；示例摄像头A（示例房间，离线）。")

    def test_camera_template_handles_empty_catalog(self):
        self.assertEqual(camera_inventory_zh([]), "设备目录里没有找到category为camera的摄像头。")

    def test_disabled_plugin_is_structured_nonrecoverable_error(self):
        with patch(
            "recipe_adapters.ExternalHomeClient.from_env",
            side_effect=ExternalHomeConfigError("disabled"),
        ):
            with self.assertRaises(RecipeExecutionError) as caught:
                external_home_device_list()
        self.assertEqual(caught.exception.code, "data_source_disabled")
        self.assertFalse(caught.exception.fallback_allowed)

    def test_http_failure_is_structured_recoverable_error(self):
        backend = Mock()
        backend.list_devices.side_effect = TimeoutError("offline")
        with patch("recipe_adapters.ExternalHomeClient.from_env", return_value=backend):
            with self.assertRaises(RecipeExecutionError) as caught:
                external_home_device_list()
        self.assertEqual(caught.exception.code, "data_source_unavailable")
        self.assertTrue(caught.exception.fallback_allowed)


if __name__ == "__main__":
    unittest.main()
