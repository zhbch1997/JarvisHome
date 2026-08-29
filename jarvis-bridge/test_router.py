import unittest

from router import Route, Router


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.router = Router()

    def test_native_speaker_commands_are_the_only_deterministic_routes(self):
        self.assertEqual(self.router.decide("播放周杰伦").route, Route.NATIVE)
        self.assertEqual(self.router.decide("把音量调到三十").route, Route.NATIVE)

    def test_normal_semantics_are_never_classified_by_rules(self):
        for text in (
            "每天晚上九点提醒我看看宠物仓鼠的饮水器",
            "宠物仓鼠在干嘛",
            "现在实时看看宠物仓鼠",
            "打开示例房间的灯",
            "查询空调现在多少度",
            "讲个笑话",
        ):
            decision = self.router.decide(text)
            self.assertEqual(decision.route, Route.LOCAL_CHAT, text)
            self.assertEqual(decision.rule_id, "model_unavailable", text)


if __name__ == "__main__":
    unittest.main()
