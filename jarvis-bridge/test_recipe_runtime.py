import unittest

from capability_defaults import (
    camera_inventory_bundle, hamster_recent_activity_bundle,
    turtle_recent_activity_bundle,
)
from recipe_adapters import (
    hamster_recent_activity_zh, parse_miloco_perception_logs,
    turtle_recent_activity_zh,
)
from recipe_runtime import RecipeExecutionError, RecipeRuntime


class RecipeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.devices = [
            {"name": "宠物龟摄像头", "room": "客厅", "category": "camera", "online": True},
            {"name": "小米智能门铃3", "room": "门口", "category": "video-doorbell", "online": True},
            {"name": "示例摄像头A", "room": "示例房间", "category": "camera", "online": True},
            {"name": "示例摄像头C", "room": "客厅", "category": "camera", "online": False},
        ]

    def test_bundle_recipe_drives_tool_filter_and_template_without_capability_branch(self):
        runtime = RecipeRuntime(
            tools={"miloco.device_list": lambda: self.devices},
            templates={"camera_inventory_zh": lambda rows: (
                f"家里一共{len(rows)}台摄像头：" + "；".join(item["name"] for item in rows) + "。"
            )},
        )

        result = runtime.execute(camera_inventory_bundle())

        self.assertEqual(result, "家里一共3台摄像头：宠物龟摄像头；示例摄像头A；示例摄像头C。")

    def test_unregistered_tool_fails_closed(self):
        bundle = camera_inventory_bundle()
        bundle["recipe"]["tool"] = "unknown.tool"
        runtime = RecipeRuntime(tools={}, templates={"camera_inventory_zh": lambda rows: "never"})

        with self.assertRaisesRegex(RecipeExecutionError, "unregistered tool") as caught:
            runtime.execute(bundle)
        self.assertFalse(caught.exception.fallback_allowed)

    def test_registered_data_source_error_preserves_structured_fallback_permission(self):
        failure = RecipeExecutionError(
            "data_source_unavailable", "Miloco设备目录读取失败", fallback_allowed=True,
        )
        runtime = RecipeRuntime(
            tools={"miloco.device_list": lambda: (_ for _ in ()).throw(failure)},
            templates={"camera_inventory_zh": lambda rows: "never"},
        )

        with self.assertRaises(RecipeExecutionError) as caught:
            runtime.execute(camera_inventory_bundle())

        self.assertEqual(caught.exception.code, "data_source_unavailable")
        self.assertTrue(caught.exception.fallback_allowed)

    def test_unregistered_transform_fails_closed(self):
        bundle = camera_inventory_bundle()
        bundle["recipe"]["transforms"] = [{"op": "arbitrary_code", "field": "category", "value": "camera"}]
        runtime = RecipeRuntime(
            tools={"miloco.device_list": lambda: self.devices},
            templates={"camera_inventory_zh": lambda rows: "never"},
        )

        with self.assertRaisesRegex(RecipeExecutionError, "unregistered transform"):
            runtime.execute(bundle)

    def test_unregistered_template_fails_closed(self):
        bundle = camera_inventory_bundle()
        bundle["recipe"]["response_template"] = "unknown_template"
        runtime = RecipeRuntime(tools={"miloco.device_list": lambda: self.devices}, templates={})

        with self.assertRaisesRegex(RecipeExecutionError, "unregistered template"):
            runtime.execute(bundle)

    def test_pet_recent_log_parser_filters_one_room_without_live_query(self):
        stdout = (
            '2026-07-29T16:00:00+08:00: {"客厅":"一只龟在晒背","示例房间":"宠物仓鼠在跑轮"}\n'
            '2026-07-29T16:01:00+08:00: {"客厅":"两只龟在晒背"}\n'
            'invalid\n'
        )
        self.assertEqual(parse_miloco_perception_logs(stdout, "示例房间"), [{
            "time": "2026-07-29T16:00:00+08:00", "description": "宠物仓鼠在跑轮",
        }])

    def test_pet_recent_answer_uses_only_the_latest_record(self):
        rows = [
            {"time": "2026-07-29T16:00:00+08:00", "description": "宠物仓鼠在跑轮"},
            {"time": "2026-07-29T16:10:00+08:00", "description": "没有看到宠物仓鼠"},
            {"time": "2026-07-29T16:20:00+08:00", "description": "宠物仓鼠正在喝水"},
        ]

        answer = hamster_recent_activity_zh(rows)

        self.assertIn("宠物仓鼠正在喝水", answer)
        self.assertNotIn("宠物仓鼠在跑轮", answer)
        self.assertNotIn("没有看到宠物仓鼠", answer)

    def test_pet_bundles_execute_only_their_fixed_read_source(self):
        turtle_rows = [{
            "time": "2026-07-29T16:01:00+08:00", "description": "两只龟在晒背",
        }]
        hamster_rows = [{
            "time": "2026-07-29T16:00:00+08:00", "description": "宠物仓鼠在跑轮",
        }]
        calls = []
        runtime = RecipeRuntime(
            tools={
                "miloco.turtle_recent": lambda: calls.append("turtle") or turtle_rows,
                "miloco.hamster_recent": lambda: calls.append("hamster") or hamster_rows,
            },
            templates={
                "turtle_recent_activity_zh": turtle_recent_activity_zh,
                "hamster_recent_activity_zh": hamster_recent_activity_zh,
            },
        )
        turtle = runtime.execute(turtle_recent_activity_bundle())
        hamster = runtime.execute(hamster_recent_activity_bundle())
        self.assertEqual(calls, ["turtle", "hamster"])
        self.assertIn("两只龟在晒背", turtle)
        self.assertNotIn("宠物仓鼠", turtle)
        self.assertIn("宠物仓鼠在跑轮", hamster)
        self.assertNotIn("龟", hamster)


if __name__ == "__main__":
    unittest.main()
