import json
import unittest

from home_scene_control import (
    SceneActionError,
    parse_scene_list,
    trigger_ac_sleep_scene,
)


class HomeSceneControlTests(unittest.TestCase):
    SCENES = json.dumps({"scenes": [
        {"scene_id": "unsafe", "scene_name": "全屋-晚安"},
        {"scene_id": "24", "scene_name": "空调睡眠24度"},
        {"scene_id": "25", "scene_name": "空调睡眠25度"},
        {"scene_id": "26", "scene_name": "空调睡眠26度"},
        {"scene_id": "sleep", "scene_name": "晚安"},
        {"scene_id": "prepare", "scene_name": "准备睡觉"},
        {"scene_id": "wake", "scene_name": "卧室-起床"},
        {"scene_id": "tv-on", "scene_name": "打开电视"},
        {"scene_id": "tv-off", "scene_name": "关闭电视"},
        {"scene_id": "air", "scene_name": "起居室新风"},
        {"scene_id": "pig", "scene_name": "早安"},
        {"scene_id": "elevator", "scene_name": "叫电梯"},
    ]}, ensure_ascii=False)

    def test_parser_keeps_only_bounded_air_conditioner_sleep_scenes(self):
        self.assertEqual(
            [(scene.scene_id, scene.name) for scene in parse_scene_list(self.SCENES)],
            [
                ("24", "空调睡眠24度"), ("25", "空调睡眠25度"),
                ("26", "空调睡眠26度"), ("sleep", "晚安"),
                ("prepare", "准备睡觉"), ("wake", "卧室-起床"),
                ("tv-on", "打开电视"), ("tv-off", "关闭电视"),
                ("air", "起居室新风"), ("pig", "早安"),
            ],
        )

    def test_4b_can_select_only_catalog_index_and_bridge_triggers_real_id(self):
        calls = []
        def cli(args):
            calls.append(args)
            return self.SCENES if args == ["scene", "list"] else '{"ok":true}'
        result = trigger_ac_sleep_scene(
            "空调睡眠24度，仅限主卧",
            cli=cli,
            ask=lambda payload: {"scene_index": 0},
        )
        self.assertEqual(calls, [["scene", "list"], ["scene", "trigger", "24"]])
        self.assertEqual(result, "好的，已执行空调睡眠24度场景。")

    def test_4b_cannot_invent_scene_index(self):
        with self.assertRaisesRegex(SceneActionError, "目录外场景"):
            trigger_ac_sleep_scene(
                "执行空调睡眠24度场景",
                cli=lambda args: self.SCENES,
                ask=lambda payload: {"scene_index": 99},
            )

    def test_request_without_allowed_scene_is_rejected_even_if_model_selects_one(self):
        with self.assertRaisesRegex(SceneActionError, "不属于已授权"):
            trigger_ac_sleep_scene(
                "执行离家场景",
                cli=lambda args: self.SCENES,
                ask=lambda payload: {"scene_index": 0},
            )

    def test_newly_authorized_scene_uses_real_catalog_id(self):
        calls = []
        def cli(args):
            calls.append(args)
            return self.SCENES if args == ["scene", "list"] else '{"ok":true}'
        result = trigger_ac_sleep_scene(
            "执行晚安场景", cli=cli,
            ask=lambda payload: {"scene_index": 3},
        )
        self.assertEqual(calls[-1], ["scene", "trigger", "sleep"])
        self.assertEqual(result, "好的，已执行晚安场景。")


if __name__ == "__main__":
    unittest.main()
