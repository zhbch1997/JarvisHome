import asyncio
import json
import unittest

from api_server import Route, RouteDecision, _agent_extra_prompt, _clean_voice_agent_result, _progress_phrases, _sse_event, _with_progress


BASE = {
    "id": "chatcmpl-test",
    "object": "chat.completion.chunk",
    "created": 0,
    "model": "jarvis",
}


def contents(chunks):
    result = []
    for chunk in chunks:
        if not isinstance(chunk, str) or not chunk.startswith("data: {"):
            continue
        payload = json.loads(chunk[6:])
        value = payload["choices"][0]["delta"].get("content")
        if value:
            result.append(value)
    return result


async def delayed_source(delay, text="已经完成。"):
    await asyncio.sleep(delay)
    yield _sse_event(BASE, {"content": text})
    yield _sse_event(BASE, {}, "stop")
    yield "data: [DONE]\n\n"


class ProgressStreamTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, delay):
        chunks = []
        async for chunk in _with_progress(
            delayed_source(delay),
            BASE,
            ("收到。", "还在处理。", "再等一下。"),
            ack_delay=0.002,
            progress_after=0.02,
            long_wait_after=0.05,
        ):
            chunks.append(chunk)
        return contents(chunks)

    async def test_fast_result_skips_redundant_ack(self):
        self.assertEqual(await self.collect(0), ["已经完成。"])

    async def test_short_result_has_ack_then_final_only(self):
        self.assertEqual(await self.collect(0.005), ["收到。", "已经完成。"])

    async def test_medium_result_adds_one_progress(self):
        self.assertEqual(
            await self.collect(0.03),
            ["收到。", "还在处理。", "已经完成。"],
        )

    async def test_slow_result_adds_bounded_long_wait(self):
        self.assertEqual(
            await self.collect(0.07),
            ["收到。", "还在处理。", "再等一下。", "已经完成。"],
        )

    def test_phrases_are_intent_specific(self):
        camera = RouteDecision(Route.CAMERA, "camera_recent", "semantic_camera_recent")
        action = RouteDecision(Route.HOME, "action", "semantic_home")
        query = RouteDecision(Route.HOME, "query", "semantic_home")
        self.assertIn("看一下", _progress_phrases(camera)[0])
        self.assertIn("处理", _progress_phrases(action)[0])
        self.assertIn("查一下", _progress_phrases(query)[0])
        web = RouteDecision(Route.WEB_QUERY, "query", "semantic_web_query")
        self.assertEqual(
            _progress_phrases(web),
            (
                "好的主人，我查一下。",
                "OpenClaw正在查询并整理信息。",
                "信息还在查询，你再等我一下。",
            ),
        )
        chat = RouteDecision(Route.LOCAL_CHAT, "chat", "semantic_chat")
        self.assertEqual(
            _progress_phrases(chat),
            (
                "好的主人，我想一下。",
                "本地 9B 正在整理回答。",
                "回答还在生成，你再等我一下。",
            ),
        )

    def test_openclaw_home_prompt_requires_grounded_pet_recent_logs(self):
        prompt = _agent_extra_prompt(Route.HOME)
        self.assertIn("external_home-perception", prompt)
        self.assertIn("perceive logs --since", prompt)
        self.assertIn("不得重新打开摄像头", prompt)
        self.assertIn("禁止猜测", prompt)

    def test_openclaw_home_prompt_counts_only_camera_category_from_device_list(self):
        prompt = _agent_extra_prompt(Route.HOME)
        self.assertIn("device list", prompt)
        self.assertIn("category严格等于camera", prompt)
        self.assertIn("不得把video-doorbell", prompt)

    def test_cleaner_keeps_final_chinese_answer_after_internal_english(self):
        raw = (
            "The query shows that the 饲养灯 is currently on (value: true)."
            "饲养灯现在是开着的。"
        )
        self.assertEqual(_clean_voice_agent_result(raw), "饲养灯现在是开着的。")

    def test_cleaner_removes_trailing_tool_failure_noise(self):
        raw = "饲养灯现在是开着的。 ⚠️ 🛠️ run node script external_home-cli.js (agent) failed"
        self.assertEqual(_clean_voice_agent_result(raw), "饲养灯现在是开着的。")

    def test_cleaner_keeps_only_natural_reply_after_internal_chinese_summary(self):
        raw = (
            "查询结果：饲养灯开着，亮度 100%，颜色值 8555520，模式 0。"
            "需要给用户简洁的回复。饲养灯开着呢，亮度拉满（100%）。"
        )
        self.assertEqual(
            _clean_voice_agent_result(raw),
            "饲养灯开着呢，亮度拉满（100%）。",
        )

    def test_cleaner_removes_web_query_internal_self_narration(self):
        raw = (
            "很好！我已经获取到了今天A股主要指数的表现数据。"
            "现在我可以给用户一个简洁的回答了。 "
            "根据数据：上证指数上涨1.15%，创业板指上涨3.16%。"
        )
        self.assertEqual(
            _clean_voice_agent_result(raw),
            "根据数据：上证指数上涨1.15%，创业板指上涨3.16%。",
        )

    def test_cleaner_removes_perception_log_planning_and_keeps_pet_result(self):
        raw = (
            "太好了，我得到了大量的感知日志记录。从这些记录可以看出：缸里有5只龟。"
            "根据感知日志，我可以用自然语言描述一下宠物龟们现在的状态。"
            "根据技能文档，我不需要逐条复述每条记录，而是归纳总结。"
            "最终回复：宠物龟们状态正常，5只都在缸里活动，有的晒背，有的游水。"
        )
        self.assertEqual(
            _clean_voice_agent_result(raw),
            "宠物龟们状态正常，5只都在缸里活动，有的晒背，有的游水。",
        )

    def test_cleaner_removes_camera_inventory_self_narration(self):
        raw = (
            "根据执行结果，确实有3台摄像头：宠物龟摄像头、示例摄像头A、示例摄像头C。"
            "用户问的是数量和清单，我需要按真实情况回答。"
            "家里一共3台摄像头：宠物龟摄像头、示例摄像头A和示例摄像头C，都是在线状态。"
        )
        self.assertEqual(
            _clean_voice_agent_result(raw),
            "家里一共3台摄像头：宠物龟摄像头、示例摄像头A和示例摄像头C，都是在线状态。",
        )


if __name__ == "__main__":
    unittest.main()
