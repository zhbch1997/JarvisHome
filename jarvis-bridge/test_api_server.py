import asyncio
import json
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, AsyncMock, patch

from fastapi.testclient import TestClient
from arbitration import ArbitrationEnvelope, Level1Decision, Level2Decision
from capability_defaults import camera_inventory_bundle
from router import RouteDecision


class ApiAuthTests(unittest.TestCase):
    def setUp(self):
        os.environ["JARVIS_API_KEY"] = "test-secret"
        import importlib
        import api_server
        self.module = importlib.reload(api_server)
        self.production_registry_wired = (
            self.module.intent_router.classifier.capability_registry
            is self.module.capability_registry
        )
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.module.feedback_ledger = self.module.FeedbackLedger(Path(self._temp.name) / "feedback")
        self.module.capability_registry = self.module.CapabilityRegistry(Path(self._temp.name) / "capabilities")
        self.module.install_defaults(self.module.capability_registry, external_home_enabled=True)
        self.module.intent_router.classifier.capability_registry = self.module.capability_registry
        self.module._EVOLUTION_STATE = Path(self._temp.name) / "evolution"
        self.module._EVOLUTION_AUDIT = self.module._EVOLUTION_STATE / "manual-tier-audit.jsonl"
        self.module._OPENCLAW_PROMPT_ROOT = Path(self._temp.name) / "openclaw"
        self.module._OPENCLAW_PROMPT_ROOT.mkdir(parents=True)
        (self.module._OPENCLAW_PROMPT_ROOT / "AGENTS.md").write_text(
            "Jarvis OpenClaw test prompt", encoding="utf-8",
        )
        self.client = TestClient(self.module.app)

    def test_scene_quick_tool_uses_dedicated_local_4b_executor(self):
        plan = self.module.ExecutionPlan(
            "home_scene_action", 1, "local_4b_scene", "local_4b",
            "execution", "", "", "",
        )
        with patch.object(
            self.module, "trigger_ac_sleep_scene",
            return_value="好的，已执行空调睡眠24度场景。",
            create=True,
        ) as trigger:
            result = self.module._execute_quick_tool(plan, "空调睡眠24度")
        self.assertEqual(result, "好的，已执行空调睡眠24度场景。")
        trigger.assert_called_once_with("空调睡眠24度")

    def test_evolution_dashboard_page_is_served_without_cache(self):
        original = self.module._EVOLUTION_DASHBOARD
        page = Path(self._temp.name) / "evolution_dashboard.html"
        page.write_text("<html><body>Evolution Dashboard 0.8B 4B OPENCLAW</body></html>", encoding="utf-8")
        self.module._EVOLUTION_DASHBOARD = page
        self.addCleanup(setattr, self.module, "_EVOLUTION_DASHBOARD", original)
        response = self.client.get("/evolution")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Evolution Dashboard", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_evolution_dashboard_is_authenticated_and_projects_three_real_tiers(self):
        unauthorized = self.client.get("/v1/evolution/dashboard")
        self.assertEqual(unauthorized.status_code, 401)

        response = self.client.get(
            "/v1/evolution/dashboard",
            headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [column["id"] for column in payload["columns"]],
            ["08b_eligible", "4b_eligible", "openclaw_only"],
        )
        self.assertIn("你是Jarvis一级仲裁器", payload["columns"][0]["prompt"]["text"])
        self.assertIn("你是Jarvis二级仲裁器", payload["columns"][1]["prompt"]["text"])
        self.assertIn("Jarvis OpenClaw test prompt", payload["columns"][2]["prompt"]["text"])
        self.assertEqual(payload["architecture"]["flow"], [
            "08b_eligible", "4b_eligible", "openclaw_only",
        ])
        self.assertEqual(
            [column["role"] for column in payload["columns"]],
            ["一级仲裁与认证直选", "二级仲裁与受约束执行", "贾维斯本人 / 开放任务执行"],
        )
        device = next(
            tool for column in payload["columns"] for tool in column["tools"]
            if tool["id"] == "home_device_action"
        )
        self.assertEqual(device["executor"], "local_4b_device")
        self.assertEqual(device["producer"], "local_4b")
        lifecycle = device["lifecycle"]
        self.assertEqual(lifecycle["stage"], "evidence")
        self.assertEqual(lifecycle["active_version"], 1)
        self.assertEqual(lifecycle["candidate_version"], 0)
        self.assertEqual(lifecycle["previous_versions"], [])
        self.assertEqual(lifecycle["validation"]["status"], "not_run")
        self.assertEqual(
            [gate["key"] for gate in lifecycle["validation"]["gates"]],
            ["replay", "dangerous_negatives", "shadow", "security"],
        )
        projected = {
            tool["id"]: column["id"]
            for column in payload["columns"] for tool in column["tools"]
        }
        for bundle in self.module.capability_registry.list_active():
            self.assertEqual(projected[bundle["id"]], bundle["selector"]["tier"])

    def test_manual_adjacent_tier_change_creates_audited_candidate_only(self):
        capability = next(
            bundle for bundle in self.module.capability_registry.list_active()
            if bundle["selector"]["tier"] == "08b_eligible"
        )
        capability_id = capability["id"]
        active_version = capability["version"]
        response = self.client.post(
            f"/v1/evolution/capabilities/{capability_id}/tier",
            headers={"Authorization": "Bearer test-secret"},
            json={"target_tier": "4b_eligible", "reason": "生产测试手工降级"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "proposed")
        active = self.module.capability_registry.active(capability_id)
        candidate = self.module.capability_registry.candidate(capability_id)
        self.assertEqual(active["version"], active_version)
        self.assertEqual(active["selector"]["tier"], "08b_eligible")
        self.assertEqual(candidate["capability"]["selector"]["tier"], "4b_eligible")
        audit = json.loads(self.module._EVOLUTION_AUDIT.read_text(encoding="utf-8").strip())
        self.assertEqual(audit["capability_id"], capability_id)
        self.assertEqual(audit["target_tier"], "4b_eligible")
        self.assertEqual(self.module._EVOLUTION_AUDIT.stat().st_mode & 0o777, 0o600)

    def test_manual_candidate_can_be_discarded_without_changing_active(self):
        capability = next(
            bundle for bundle in self.module.capability_registry.list_active()
            if bundle["selector"]["tier"] == "08b_eligible"
        )
        capability_id = capability["id"]
        headers = {"Authorization": "Bearer test-secret"}
        proposed = self.client.post(
            f"/v1/evolution/capabilities/{capability_id}/tier", headers=headers,
            json={"target_tier": "4b_eligible", "reason": "准备撤销的生产测试"},
        )
        self.assertEqual(proposed.status_code, 200)
        discarded = self.client.request(
            "DELETE", f"/v1/evolution/capabilities/{capability_id}/candidate",
            headers=headers, json={"reason": "撤销这次测试"},
        )
        self.assertEqual(discarded.status_code, 200, discarded.text)
        self.assertIsNone(self.module.capability_registry.candidate(capability_id))
        self.assertEqual(
            self.module.capability_registry.active(capability_id)["version"],
            capability["version"],
        )
        events = [
            json.loads(line) for line in self.module._EVOLUTION_AUDIT.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([event["event"] for event in events], ["tier_proposed", "candidate_discarded"])

    def test_manual_tier_change_rejects_skips_and_bad_reason(self):
        capability = next(
            bundle for bundle in self.module.capability_registry.list_active()
            if bundle["selector"]["tier"] == "08b_eligible"
        )
        headers = {"Authorization": "Bearer test-secret"}
        skipped = self.client.post(
            f"/v1/evolution/capabilities/{capability['id']}/tier", headers=headers,
            json={"target_tier": "openclaw_only", "reason": "不可跨级"},
        )
        malformed = self.client.post(
            f"/v1/evolution/capabilities/{capability['id']}/tier", headers=headers,
            json={"target_tier": "4b_eligible", "reason": "x"},
        )
        self.assertEqual(skipped.status_code, 400)
        self.assertEqual(malformed.status_code, 400)
        self.assertIsNone(self.module.capability_registry.candidate(capability["id"]))

    def test_semantic_classifier_uses_the_runtime_capability_registry(self):
        self.assertTrue(self.production_registry_wired)

    def test_startup_warms_both_arbitration_models_with_permanent_keep_alive(self):
        calls = []

        async def capture(url, model):
            calls.append((url, model))

        with patch.object(self.module, "_warm_ollama_model", side_effect=capture):
            asyncio.run(self.module.warm_arbitration_models())
        self.assertCountEqual(calls, [
            ("http://127.0.0.1:11435/api/generate", "qwen35-router:0.8b"),
            ("http://127.0.0.1:11434/api/generate", "qwen35-4b-16k:latest"),
        ])

    def test_generic_openclaw_handoff_does_not_impersonate_home_route(self):
        prompt = self.module._agent_extra_prompt(None)
        self.assertIn("理解用户的开放式请求", prompt)
        self.assertNotIn("只处理明确的家庭设备", prompt)
        self.assertNotIn("只处理明确的家庭任务", prompt)
        self.assertTrue(self.module._web_query_has_observed_tool(None, []))

    def test_level1_transition_is_emitted_before_level2_finishes(self):
        parent = ArbitrationEnvelope(
            arbitration_id="arb-immediate",
            level1=Level1Decision("handoff", "none", "lookup", 0.98),
            level2_required=True,
        )
        release_level2 = asyncio.Event()
        level2_started = asyncio.Event()

        async def delayed_level2(_text, envelope):
            level2_started.set()
            await release_level2.wait()
            return ArbitrationEnvelope(
                arbitration_id=envelope.arbitration_id,
                level1=envelope.level1,
                level2_required=True,
                level2=Level2Decision("openclaw", "none", "escalated", 0.97),
            )

        async def verify_order():
            base = {
                "id": "chatcmpl-jarvis", "object": "chat.completion.chunk",
                "created": 0, "model": "hermes-jarvis",
            }
            with patch.object(self.module.level1_arbitrator, "decide", AsyncMock(return_value=parent)), \
                 patch.object(self.module.level2_arbitrator, "decide", side_effect=delayed_level2):
                stream = self.module._two_stage_stream({
                    "session_id": "immediate-transition",
                    "messages": [{"role": "user", "content": "查一下天气"}],
                }, base)
                first_pending = asyncio.create_task(stream.__anext__())
                await asyncio.sleep(0.02)
                first_arrived_before_level2 = first_pending.done()
                first = await first_pending
                second = await stream.__anext__()
                self.assertTrue(first_arrived_before_level2)
                self.assertIn('\"phase\":\"arbitration_l1\"', first)
                self.assertIn("好的主人，我查一下。", second)
                self.assertFalse(release_level2.is_set())
                release_level2.set()
                await stream.aclose()

        asyncio.run(verify_order())

    def test_two_stage_chat_skips_level2_and_openclaw(self):
        parent = ArbitrationEnvelope(
            arbitration_id="arb-chat",
            level1=Level1Decision("chat", "none", "none", 0.99),
            level2_required=False,
        )

        async def fake_chat(_body):
            base = {
                "id": "chatcmpl-chat", "object": "chat.completion.chunk",
                "created": 0, "model": "qwen35-4b-16k:latest",
            }
            yield self.module._sse_event(base, {"content": "你好，主人。"})
            yield self.module._sse_event(base, {}, "stop")
            yield "data: [DONE]\n\n"

        with patch.object(self.module, "TWO_STAGE_ARBITRATION", True), \
             patch.object(self.module.level1_arbitrator, "decide", AsyncMock(return_value=parent)), \
             patch.object(self.module.level2_arbitrator, "decide", AsyncMock()) as level2, \
             patch.object(self.module.local_chat, "stream", side_effect=fake_chat), \
             patch.object(self.module, "_openclaw_ws_stream", side_effect=AssertionError("must not use OpenClaw")):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "stream": True, "session_id": "two-stage-chat",
                    "messages": [{"role": "user", "content": "你好"}],
                },
            )
        self.assertIn("你好，主人。", response.text)
        level2.assert_not_awaited()
        turn = self.module.feedback_ledger.session_turns("two-stage-chat")[-1]
        self.assertEqual(turn["arbitration_id"], "arb-chat")
        self.assertEqual(turn["execution_class"], "local_chat")
        self.assertEqual((turn["route"], turn["intent"]), ("arbitrated", "local_chat"))
        self.assertIsNone(turn["level2"])

    def test_two_stage_openclaw_uses_generic_handoff_and_never_quick_tool(self):
        parent = ArbitrationEnvelope(
            arbitration_id="arb-openclaw",
            level1=Level1Decision("handoff", "none", "general", 0.98),
            level2_required=True,
        )
        child = ArbitrationEnvelope(
            arbitration_id="arb-openclaw", level1=parent.level1,
            level2_required=True,
            level2=Level2Decision("openclaw", "none", "escalated", 0.97),
        )
        observed_routes = []

        async def fake_openclaw(_body, model_id, route):
            observed_routes.append(route)
            base = {
                "id": "chatcmpl-openclaw", "object": "chat.completion.chunk",
                "created": 0, "model": model_id,
            }
            yield self.module._sse_event(base, {"content": "已完成开放任务。"})
            yield self.module._sse_event(base, {}, "stop")
            yield "data: [DONE]\n\n"

        with patch.object(self.module, "TWO_STAGE_ARBITRATION", True), \
             patch.object(self.module.level1_arbitrator, "decide", AsyncMock(return_value=parent)), \
             patch.object(self.module.level2_arbitrator, "decide", AsyncMock(return_value=child)), \
             patch.object(self.module.recipe_runtime, "execute", side_effect=AssertionError("must not execute Quick Tool")), \
             patch.object(self.module, "_openclaw_ws_stream", side_effect=fake_openclaw):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "stream": True, "session_id": "two-stage-openclaw",
                    "messages": [{"role": "user", "content": "帮我研究一个开放问题"}],
                },
            )
        self.assertIn("已完成开放任务。", response.text)
        self.assertEqual(observed_routes, [None])
        turn = self.module.feedback_ledger.session_turns("two-stage-openclaw")[-1]
        self.assertEqual(turn["arbitration_id"], "arb-openclaw")
        self.assertEqual(turn["execution_class"], "openclaw")
        self.assertEqual((turn["route"], turn["intent"]), ("arbitrated", "openclaw"))
        self.assertEqual(turn["level2"]["relation"], "escalated")

    def test_two_stage_non_stream_uses_same_envelope_and_skips_legacy_router(self):
        bundle = camera_inventory_bundle()
        bundle["version"] = 2
        bundle["selector"] = {
            "tier": "4b_eligible", "description": "查询家庭摄像头目录",
            "positive_examples": ["家里有几台摄像头"],
            "dangerous_negatives": ["现在打开摄像头"],
            "min_confidence": {"0.8b": 0.99, "4b": 0.95},
        }
        bundle["taxonomy"] = {
            "domain_tags": ["home", "camera"],
            "execution_kind": "deterministic_query",
        }
        self.module.capability_registry = self.module.CapabilityRegistry(
            Path(self._temp.name) / "two-stage-non-stream-capabilities"
        )
        self.module.capability_registry.install_initial(bundle)
        parent = ArbitrationEnvelope(
            arbitration_id="arb-non-stream",
            level1=Level1Decision("handoff", "none", "lookup", 0.98),
            level2_required=True,
        )
        child = ArbitrationEnvelope(
            arbitration_id="arb-non-stream", level1=parent.level1,
            level2_required=True,
            level2=Level2Decision(
                "quick_tool", "camera_inventory", "consistent", 0.97,
            ),
        )
        body = {
            "model": "jarvis", "session_id": "two-stage-non-stream",
            "messages": [{"role": "user", "content": "家里有几台摄像头"}],
        }
        with patch.object(self.module, "TWO_STAGE_ARBITRATION", True), \
             patch.object(self.module.level1_arbitrator, "decide", AsyncMock(return_value=parent)), \
             patch.object(self.module.level2_arbitrator, "decide", AsyncMock(return_value=child)), \
             patch.object(self.module.intent_router, "decide", AsyncMock(side_effect=AssertionError("must not use legacy router"))), \
             patch.object(self.module.recipe_runtime, "execute", return_value="家里一共3台摄像头。"):
            result = asyncio.run(self.module._complete_chat(body))
        self.assertEqual(
            result["choices"][0]["message"]["content"],
            "家里一共3台摄像头。",
        )
        turn = self.module.feedback_ledger.session_turns("two-stage-non-stream")[-1]
        self.assertEqual(turn["arbitration_id"], "arb-non-stream")
        self.assertEqual(turn["execution_class"], "quick_tool")
        self.assertEqual(turn["decision_tier"], "4b")
        self.assertEqual(turn["level2"]["relation"], "consistent")
        self.assertEqual((turn["route"], turn["intent"]), ("arbitrated", "quick_tool"))

    def test_two_stage_device_quick_tool_uses_constrained_local_4b_executor(self):
        parent = ArbitrationEnvelope(
            arbitration_id="arb-device-action",
            level1=Level1Decision("handoff", "none", "action", 0.99),
            level2_required=True,
        )
        child = ArbitrationEnvelope(
            arbitration_id=parent.arbitration_id,
            level1=parent.level1,
            level2_required=True,
            level2=Level2Decision(
                "quick_tool", "home_device_action", "consistent", 0.99,
            ),
        )
        body = {
            "model": "jarvis", "session_id": "device-action",
            "messages": [{"role": "user", "content": "打开客厅灯"}],
        }
        with patch.object(self.module, "TWO_STAGE_ARBITRATION", True), \
             patch.object(self.module.level1_arbitrator, "decide", AsyncMock(return_value=parent)), \
             patch.object(self.module.level2_arbitrator, "decide", AsyncMock(return_value=child)), \
             patch.object(self.module, "control_device", return_value="好的，已打开客厅灯。") as control, \
             patch.object(self.module.recipe_runtime, "execute", side_effect=AssertionError("must not use generic recipe")), \
             patch.object(self.module, "run_openclaw", side_effect=AssertionError("must not use OpenClaw")):
            result = asyncio.run(self.module._complete_chat(body))
        self.assertEqual(result["choices"][0]["message"]["content"], "好的，已打开客厅灯。")
        control.assert_called_once_with("打开客厅灯")
        turn = self.module.feedback_ledger.session_turns("device-action")[-1]
        self.assertEqual(turn["executor"], "local_4b_device")
        self.assertEqual(turn["capability"], "home_device_action")

    def test_two_stage_non_stream_openclaw_resolves_missing_session_before_execution(self):
        parent = ArbitrationEnvelope(
            arbitration_id="arb-non-stream-openclaw",
            level1=Level1Decision("handoff", "none", "lookup", 0.98),
            level2_required=True,
        )
        child = ArbitrationEnvelope(
            arbitration_id=parent.arbitration_id, level1=parent.level1,
            level2_required=True,
            level2=Level2Decision("openclaw", "none", "escalated", 0.97),
        )
        observed = {}

        async def fake_run(body, route, tool_trace=None):
            observed.update(body)
            self.assertIsNone(route)
            return "只读结果。"

        with patch.object(self.module.level1_arbitrator, "decide", AsyncMock(return_value=parent)), \
             patch.object(self.module.level2_arbitrator, "decide", AsyncMock(return_value=child)), \
             patch.object(self.module, "run_openclaw", side_effect=fake_run):
            result = asyncio.run(self.module._complete_two_stage({
                "model": "jarvis",
                "messages": [{"role": "user", "content": "查一个开放问题"}],
            }))
        self.assertTrue(observed["session_id"])
        self.assertEqual(observed["messages"][-1]["content"], "查一个开放问题")
        turn = self.module.feedback_ledger.session_turns(observed["session_id"])[-1]
        self.assertEqual((turn["route"], turn["intent"]), ("arbitrated", "openclaw"))
        self.assertEqual(result["choices"][0]["message"]["content"], "只读结果。")

    def test_two_stage_stream_records_one_parent_child_quick_tool_trace(self):
        bundle = camera_inventory_bundle()
        bundle["version"] = 2
        bundle["selector"] = {
            "tier": "4b_eligible", "description": "查询家庭摄像头目录",
            "positive_examples": ["家里有几台摄像头"],
            "dangerous_negatives": ["现在打开摄像头"],
            "min_confidence": {"0.8b": 0.99, "4b": 0.95},
        }
        bundle["taxonomy"] = {
            "domain_tags": ["home", "camera"],
            "execution_kind": "deterministic_query",
        }
        self.module.capability_registry = self.module.CapabilityRegistry(
            Path(self._temp.name) / "two-stage-capabilities"
        )
        self.module.capability_registry.install_initial(bundle)
        envelope = ArbitrationEnvelope(
            arbitration_id="arb-two-stage",
            level1=Level1Decision("handoff", "none", "lookup", 0.98),
            level2_required=True,
            level2=Level2Decision(
                "quick_tool", "camera_inventory", "consistent", 0.97,
            ),
        )
        with patch.object(self.module, "TWO_STAGE_ARBITRATION", True), \
             patch.object(self.module.level1_arbitrator, "decide", AsyncMock(return_value=ArbitrationEnvelope(
                 arbitration_id=envelope.arbitration_id,
                 level1=envelope.level1,
                 level2_required=True,
             ))), \
             patch.object(self.module.level2_arbitrator, "decide", AsyncMock(return_value=envelope)), \
             patch.object(self.module.recipe_runtime, "execute", return_value="家里一共3台摄像头。"), \
             patch.object(self.module, "_openclaw_ws_stream", side_effect=AssertionError("must not use OpenClaw")):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "stream": True, "session_id": "two-stage-stream",
                    "messages": [{"role": "user", "content": "家里有几台摄像头"}],
                },
            )
        self.assertEqual(response.status_code, 200)
        frames = [
            self.module.json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]
        stages = [
            frame["jarvis"] for frame in frames
            if (frame.get("jarvis") or {}).get("event") == "stage"
        ]
        completed = [
            stage for stage in stages
            if stage["phase"] in {"arbitration_l1", "arbitration_l2", "execution"}
            and stage["status"] == "completed"
        ]
        self.assertEqual([stage["phase"] for stage in completed], [
            "arbitration_l1", "arbitration_l2", "execution",
        ])
        self.assertTrue(all(
            stage["data"]["arbitration_id"] == "arb-two-stage"
            for stage in completed
        ))
        self.assertEqual(completed[-1]["data"]["execution_class"], "quick_tool")
        self.assertEqual(completed[-1]["data"]["planned_executor"], "quick_tool")
        self.assertEqual(completed[-1]["data"]["execution_kind"], "deterministic_query")
        self.assertEqual(completed[-1]["data"]["risk_class"], "read_only")
        metadata = next(
            frame for frame in frames
            if (frame.get("jarvis") or {}).get("stage") == "recorded"
        )
        turn = self.module.feedback_ledger.get_turn(metadata["jarvis"]["turn_id"])
        self.assertEqual(turn["arbitration_id"], "arb-two-stage")
        self.assertEqual(turn["level1"]["decision"], "handoff")
        self.assertEqual(turn["level2"]["decision"], "quick_tool")
        self.assertEqual(turn["execution_class"], "quick_tool")
        self.assertEqual(turn["decision_tier"], "4b")
        self.assertEqual(turn["tool_class"], "deterministic_query")
        self.assertEqual(turn["risk_class"], "read_only")
        self.assertEqual((turn["route"], turn["intent"]), ("arbitrated", "quick_tool"))

    def test_airi_surface_changes_only_the_current_openclaw_interface_prompt(self):
        speaker = self.module._agent_extra_prompt(self.module.Route.HOME, surface="speaker")
        airi = self.module._agent_extra_prompt(self.module.Route.HOME, surface="airi")
        self.assertIn("小爱音箱", speaker)
        self.assertIn("数字形象界面", airi)
        self.assertNotIn("小爱音箱", airi)

    def test_health_is_public(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_chat_rejects_missing_bearer_token(self):
        response = self.client.post("/v1/chat/completions", json={"messages": []})
        self.assertEqual(response.status_code, 401)

    def test_api_key_can_be_loaded_from_file(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        import importlib
        import api_server

        with TemporaryDirectory() as d:
            key_file = Path(d) / "key"
            key_file.write_text("file-secret\n")
            os.environ.pop("JARVIS_API_KEY", None)
            os.environ["JARVIS_API_KEY_FILE"] = str(key_file)
            module = importlib.reload(api_server)
            self.assertEqual(module._api_key, "file-secret")

    def test_chat_accepts_matching_bearer_token(self):
        with patch.object(self.module, "_complete_chat", AsyncMock(return_value={"choices": []})):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={"messages": [{"role": "user", "content": "请回答"}]},
            )
        self.assertEqual(response.status_code, 200)

    def test_any_registered_capability_executes_its_bundle_recipe_without_id_branch(self):
        bundle = camera_inventory_bundle()
        bundle["id"] = "camera_catalog"
        bundle["execution"]["recipe"] = "camera_catalog_v1"
        self.module.capability_registry.install_initial(bundle)
        decision = self.module.RouteDecision(
            self.module.Route.HOME, "query", "semantic_home", "read_only", 1.0,
            capability="camera_catalog",
        )

        class FakeRuntime:
            def __init__(self):
                self.bundle = None

            def execute(self, active_bundle):
                self.bundle = active_bundle
                return "通用Recipe已执行。"

        runtime = FakeRuntime()
        with patch.object(self.module, "recipe_runtime", runtime, create=True), \
             patch.object(self.module, "run_openclaw", AsyncMock(side_effect=AssertionError("must not use OpenClaw"))):
            _result, content = asyncio.run(self.module._execute_decision({
                "model": "jarvis", "messages": [{"role": "user", "content": "读取目录"}],
            }, decision))

        self.assertEqual(content, "通用Recipe已执行。")
        self.assertEqual(runtime.bundle["id"], "camera_catalog")

    def test_non_stream_capability_records_the_same_execution_plan_and_session(self):
        decision = self.module.RouteDecision(
            self.module.Route.HOME, "query", "semantic_home", "read_only", 1.0,
            capability="camera_inventory",
        )
        body = {
            "model": "jarvis", "session_id": "non-stream-capability",
            "messages": [{"role": "user", "content": "家里有几台摄像头？"}],
        }
        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)), \
             patch.object(self.module.recipe_runtime, "execute", return_value="家里一共3台摄像头。"):
            result = asyncio.run(self.module._complete_chat(body))

        self.assertEqual(result["choices"][0]["message"]["content"], "家里一共3台摄像头。")
        turn = self.module.feedback_ledger.last_turn()
        self.assertEqual(turn["session_id"], "non-stream-capability")
        self.assertEqual(
            (turn["executor"], turn["producer"], turn["capability"], turn["capability_version"]),
            ("bridge_recipe", "external_home", "camera_inventory", 1),
        )

    def test_non_stream_capability_fails_closed_without_restricted_fallback_profile(self):
        decision = self.module.RouteDecision(
            self.module.Route.HOME, "query", "semantic_home", "read_only", 1.0,
            capability="camera_inventory",
        )
        failure = self.module.RecipeExecutionError(
            "data_source_unavailable", "External home backend不可用", fallback_allowed=True,
        )
        body = {
            "model": "jarvis", "session_id": "fallback-non-stream",
            "messages": [{"role": "user", "content": "家里有几台摄像头？"}],
        }
        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)), \
             patch.object(self.module.recipe_runtime, "execute", side_effect=failure), \
             patch.object(self.module, "run_openclaw", AsyncMock()) as fallback:
            with self.assertRaisesRegex(
                self.module.CapabilityValidationError,
                "restricted fallback profile is unavailable",
            ):
                asyncio.run(self.module._complete_chat(body))

        fallback.assert_not_awaited()
        turn = self.module.feedback_ledger.last_turn()
        self.assertFalse(turn["success"])
        self.assertEqual(turn["executor"], "bridge_recipe")
        self.assertEqual(turn["producer"], "external_home")
        self.assertEqual(turn["escalation_reason"], "")
        self.assertEqual((turn["capability"], turn["capability_version"]), ("camera_inventory", 1))

    def test_non_stream_capability_never_falls_back_for_recipe_bug(self):
        decision = self.module.RouteDecision(
            self.module.Route.HOME, "query", "semantic_home", "read_only", 1.0,
            capability="camera_inventory",
        )
        failure = self.module.RecipeExecutionError("recipe_invalid", "模板错误")
        body = {
            "model": "jarvis", "session_id": "fallback-denied",
            "messages": [{"role": "user", "content": "家里有几台摄像头？"}],
        }
        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)), \
             patch.object(self.module.recipe_runtime, "execute", side_effect=failure), \
             patch.object(self.module, "run_openclaw", AsyncMock()) as fallback:
            with self.assertRaises(self.module.CapabilityValidationError):
                asyncio.run(self.module._complete_chat(body))

        fallback.assert_not_awaited()
        turn = self.module.feedback_ledger.last_turn()
        self.assertFalse(turn["success"])
        self.assertEqual(turn["producer"], "external_home")
        self.assertEqual(turn["escalation_reason"], "")

    def test_camera_inventory_is_deterministic_external_home_execution(self):
        decision = self.module.RouteDecision(
            self.module.Route.HOME, "query", "semantic_home", "read_only", 1.0,
            capability="camera_inventory",
        )
        with patch.object(self.module.recipe_runtime, "execute", return_value="家里一共3台摄像头。") as execute, \
             patch.object(self.module, "run_openclaw", AsyncMock(side_effect=AssertionError("must not use OpenClaw"))):
            _result, content = asyncio.run(self.module._execute_decision({
                "model": "jarvis", "session_id": "camera-inventory-test",
                "messages": [{"role": "user", "content": "家里有几台摄像头？"}],
            }, decision))
        self.assertEqual(content, "家里一共3台摄像头。")
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0]["id"], "camera_inventory")

    def test_any_registered_capability_stream_uses_recipe_and_records_plan(self):
        bundle = camera_inventory_bundle()
        bundle["id"] = "camera_catalog"
        bundle["execution"]["recipe"] = "camera_catalog_v1"
        self.module.capability_registry.install_initial(bundle)
        decision = self.module.RouteDecision(
            self.module.Route.HOME, "query", "semantic_home", "read_only", 1.0,
            capability="camera_catalog",
        )

        class FakeRuntime:
            def execute(self, active_bundle):
                self.bundle = active_bundle
                return "通用流式Recipe已执行。"

        runtime = FakeRuntime()
        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)), \
             patch.object(self.module, "recipe_runtime", runtime), \
             patch.object(self.module, "_openclaw_ws_stream", side_effect=AssertionError("must not use OpenClaw")):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={"stream": True, "session_id": "generic-capability-stream", "messages": [{"role": "user", "content": "读取目录"}]},
            )

        self.assertIn("通用流式Recipe已执行。", response.text)
        frames = [self.module.json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        stages = [item["jarvis"] for item in frames if (item.get("jarvis") or {}).get("event") == "stage"]
        execution = [item for item in stages if item["phase"] == "execution"]
        self.assertEqual([(item["producer"], item["status"]) for item in execution], [("external_home", "started"), ("external_home", "completed")])
        self.assertEqual(execution[-1]["data"]["capability"], "camera_catalog")
        metadata = next(item for item in frames if (item.get("jarvis") or {}).get("stage") == "recorded")
        turn = self.module.feedback_ledger.get_turn(metadata["jarvis"]["turn_id"])
        self.assertEqual((turn["executor"], turn["producer"], turn["capability"]), ("bridge_recipe", "external_home", "camera_catalog"))

    def test_stream_capability_fails_closed_without_restricted_fallback_profile(self):
        decision = self.module.RouteDecision(
            self.module.Route.HOME, "query", "semantic_home", "read_only", 1.0,
            capability="camera_inventory",
        )
        failure = self.module.RecipeExecutionError(
            "data_source_unavailable", "External home backend不可用", fallback_allowed=True,
        )

        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)), \
             patch.object(self.module.recipe_runtime, "execute", side_effect=failure), \
             patch.object(self.module, "_openclaw_ws_stream", AsyncMock()) as fallback:
            with self.assertRaisesRegex(
                self.module.CapabilityValidationError,
                "restricted fallback profile is unavailable",
            ):
                self.client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer test-secret"},
                    json={"stream": True, "session_id": "fallback-stream", "messages": [{"role": "user", "content": "家里有几台摄像头？"}]},
                )

        fallback.assert_not_awaited()
        turns = self.module.feedback_ledger.session_turns("fallback-stream")
        self.assertEqual(len(turns), 1)
        turn = turns[0]
        self.assertFalse(turn["success"])
        self.assertEqual(turn["executor"], "bridge_recipe")
        self.assertEqual(turn["producer"], "external_home")
        self.assertEqual(turn["escalation_reason"], "")

    def test_stream_capability_never_falls_back_or_duplicates_turn_for_recipe_bug(self):
        decision = self.module.RouteDecision(
            self.module.Route.HOME, "query", "semantic_home", "read_only", 1.0,
            capability="camera_inventory",
        )
        failure = self.module.RecipeExecutionError("recipe_invalid", "模板错误")
        body = {
            "stream": True, "session_id": "stream-fallback-denied",
            "messages": [{"role": "user", "content": "家里有几台摄像头？"}],
        }

        async def consume():
            async for _ in self.module._stream_chat(body):
                pass

        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)), \
             patch.object(self.module.recipe_runtime, "execute", side_effect=failure), \
             patch.object(self.module, "_openclaw_ws_stream") as fallback:
            with self.assertRaises(self.module.CapabilityValidationError):
                asyncio.run(consume())

        fallback.assert_not_called()
        turns = self.module.feedback_ledger.session_turns("stream-fallback-denied")
        self.assertEqual(len(turns), 1)
        self.assertFalse(turns[0]["success"])
        self.assertEqual(turns[0]["producer"], "external_home")
        self.assertEqual(turns[0]["escalation_reason"], "")

    def test_camera_inventory_stream_records_actual_capability_executor_and_version(self):
        decision = self.module.RouteDecision(
            self.module.Route.HOME, "query", "semantic_home", "read_only", 1.0,
            capability="camera_inventory",
        )
        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)), \
             patch.object(self.module.recipe_runtime, "execute", return_value="家里一共3台摄像头。"), \
             patch.object(self.module, "_openclaw_ws_stream", side_effect=AssertionError("must not use OpenClaw")):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={"stream": True, "session_id": "capability-test", "messages": [{"role": "user", "content": "家里有几台摄像头？"}]},
            )
        events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        frames = [self.module.json.loads(item) for item in events if item != "[DONE]"]
        stages = [item["jarvis"] for item in frames if (item.get("jarvis") or {}).get("event") == "stage"]
        execution = [item for item in stages if item["phase"] == "execution"]
        self.assertEqual([(item["producer"], item["status"]) for item in execution], [
            ("external_home", "started"), ("external_home", "completed"),
        ])
        completed = execution[-1]["data"]
        self.assertEqual(completed["capability"], "camera_inventory")
        self.assertEqual(completed["capability_version"], 1)
        self.assertEqual(completed["planned_executor"], "bridge_recipe")
        metadata = next(item for item in frames if (item.get("jarvis") or {}).get("stage") == "recorded")
        turn = self.module.feedback_ledger.get_turn(metadata["jarvis"]["turn_id"])
        self.assertEqual(turn["executor"], "bridge_recipe")
        self.assertEqual(turn["producer"], "external_home")
        self.assertEqual(turn["capability"], "camera_inventory")
        self.assertEqual(turn["capability_version"], 1)

    def test_explicit_route_review_uses_selected_turn_and_creates_v2_candidate(self):
        selected = self.module.feedback_ledger.save_turn({
            "request": "以后每晚看看饮水器", "route": "arbitrated",
            "intent": "local_chat", "executor": "local_4b",
            "answer": "可以。", "success": True,
            "level1": {"decision": "chat", "quick_tool_id": "none", "handoff": "none", "confidence": 0.98},
            "decision_tier": "0.8b", "execution_class": "local_chat",
        })
        raw_review = json.dumps({
            "level1": {"decision": "handoff", "quick_tool_id": "none", "handoff": "action"},
            "level2": {"decision": "openclaw", "quick_tool_id": "none"},
            "confidence": 0.98, "reason": "这是持续任务，应交给OpenClaw",
        }, ensure_ascii=False)
        with patch.object(self.module, "openclaw_reviewer", return_value=raw_review) as reviewer:
            response = self.client.post(
                "/v1/route-reviews",
                headers={"Authorization": "Bearer test-secret"},
                json={"turn_id": selected["turn_id"]},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["turn_id"], selected["turn_id"])
        self.assertEqual(payload["review"]["level1"]["decision"], "handoff")
        self.assertEqual(payload["review"]["level2"]["decision"], "openclaw")
        self.assertEqual(payload["status"], "reviewed")
        self.assertTrue(payload["candidate_created"])
        reviewer.assert_called_once()
        rows = (self.module.feedback_ledger.root / "evolution-feedback-v2.jsonl").read_text().splitlines()
        saved = json.loads(rows[-1])
        self.assertEqual(saved["level1"]["handoff"], "action")
        self.assertFalse((self.module.feedback_ledger.root / "router-candidate-examples.json").exists())

    def test_explicit_route_review_rejects_legacy_review_contract(self):
        selected = self.module.feedback_ledger.save_turn({
            "request": "今天股票情况怎么样", "route": "arbitrated",
            "intent": "local_chat", "executor": "local_4b",
            "answer": "不知道。", "success": False,
        })
        legacy = '{"route":"web_query","intent":"query","executor":"openclaw","confidence":0.99,"safe_to_retry":true,"reason":"旧协议"}'
        with patch.object(self.module, "openclaw_reviewer", return_value=legacy):
            response = self.client.post(
                "/v1/route-reviews",
                headers={"Authorization": "Bearer test-secret"},
                json={"turn_id": selected["turn_id"]},
            )
        self.assertEqual(response.status_code, 400)

    def test_legacy_camera_review_cannot_create_bundle_candidate(self):
        selected = self.module.feedback_ledger.save_turn({
            "request": "监控设备都有哪些", "route": "local_chat", "intent": "chat",
            "executor": "local_9b", "answer": "我不确定。", "success": False,
        })
        raw_review = (
            '{"route":"home","intent":"query","executor":"external_home",'
            '"capability":"camera_inventory","confidence":0.98,'
            '"safe_to_retry":true,"reason":"应读取真实摄像头目录"}'
        )
        with patch.object(self.module, "openclaw_reviewer", return_value=raw_review):
            response = self.client.post(
                "/v1/route-reviews",
                headers={"Authorization": "Bearer test-secret"},
                json={"turn_id": selected["turn_id"]},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.module.capability_registry.active("camera_inventory")["version"], 1)

    def test_legacy_stock_review_cannot_create_router_candidate(self):
        selected = self.module.feedback_ledger.save_turn({
            "request": "今天股票情况怎么样", "route": "home",
            "intent": "query", "executor": "openclaw",
            "answer": "今天A股上涨。", "success": True,
        })
        raw_review = (
            '{"route":"web_query","intent":"query","executor":"openclaw",'
            '"confidence":0.99,"safe_to_retry":true,"reason":"需要外部实时行情"}'
        )
        with patch.object(self.module, "openclaw_reviewer", return_value=raw_review):
            response = self.client.post(
                "/v1/route-reviews",
                headers={"Authorization": "Bearer test-secret"},
                json={"turn_id": selected["turn_id"]},
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.module.feedback_ledger.candidate_examples(), [])

    def test_matching_v2_review_reports_no_candidate_created(self):
        selected = self.module.feedback_ledger.save_turn({
            "request": "宠物仓鼠在干嘛", "route": "arbitrated",
            "intent": "quick_tool", "executor": "bridge_recipe",
            "answer": "没有近期记录。", "success": True,
            "level1": {
                "decision": "quick_tool",
                "quick_tool_id": "hamster_recent_activity",
                "handoff": "lookup", "confidence": 0.99,
            },
            "level2": None,
        })
        raw_review = json.dumps({
            "level1": {
                "decision": "quick_tool",
                "quick_tool_id": "hamster_recent_activity",
                "handoff": "lookup",
            },
            "level2": None,
            "confidence": 0.99,
            "reason": "原路线正确",
        }, ensure_ascii=False)
        with patch.object(self.module, "openclaw_reviewer", return_value=raw_review):
            response = self.client.post(
                "/v1/route-reviews",
                headers={"Authorization": "Bearer test-secret"},
                json={"turn_id": selected["turn_id"]},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "reviewed")
        self.assertFalse(payload["candidate_created"])
        self.assertEqual(self.module.feedback_ledger.candidate_examples(), [])

    def test_capability_api_lists_active_and_inactive_candidate(self):
        active = self.client.get(
            "/v1/capabilities", headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(active.status_code, 200)
        active_summary = active.json()["active"][0]
        self.assertEqual(active_summary["id"], "camera_inventory")
        self.assertEqual(active_summary["version"], 1)
        self.assertEqual(active_summary["selector_tier"], "openclaw_only")
        self.assertEqual(active_summary["execution_kind"], "deterministic_query")
        self.assertEqual(active_summary["domain_tags"], ["home"])
        self.assertEqual(active_summary["planned_executor"], "bridge_recipe")
        self.assertEqual(active_summary["execution_class"], "quick_tool")
        self.assertNotIn("recipe", active_summary)
        self.assertNotIn("positive_examples", active_summary)
        self.assertNotIn("dangerous_negatives", active_summary)
        self.assertNotIn("min_confidence", active_summary)

        bundle = self.module.capability_registry.active("camera_inventory")
        candidate = self.module.json.loads(self.module.json.dumps(bundle))
        candidate["version"] = 2
        candidate["status"] = "proposed"
        candidate["routing"]["examples"].append("监控总共有几个")
        self.module.capability_registry.propose(candidate, ["private-turn-id"])
        payload = self.client.get(
            "/v1/capabilities", headers={"Authorization": "Bearer test-secret"},
        ).json()
        self.assertEqual(payload["candidates"], [{
            "id": "camera_inventory", "version": 2,
            "from_version": 1, "status": "proposed",
            "active_in_production": False,
            "selector_tier": "openclaw_only",
            "selector_description": "camera inventory",
            "domain_tags": ["home"],
            "execution_kind": "deterministic_query",
            "risk": "read_only",
        }])
        self.assertNotIn("evidence_ids", self.module.json.dumps(payload["candidates"]))
        self.assertNotIn("private-turn-id", self.module.json.dumps(payload))

    def test_tier_validation_api_projects_dynamic_gates_without_report_details(self):
        report = {
            "schema_version": 2,
            "change_kind": "selector_tier",
            "from_tier": "openclaw_only",
            "target_tier": "4b_eligible",
            "report_hash": "a" * 64,
            "passed": False,
            "gates": {
                "replay_4b": {"passed": True, "sample_count": 100},
                "dangerous_negatives_4b": {
                    "passed": False, "reason": "private-detail",
                },
                "shadow": {"passed": True, "active_result_hash": "private"},
                "security": {"passed": True, "tool": "private-tool"},
            },
        }

        class FakeValidator:
            def validate_candidate(self, capability_id):
                self.capability_id = capability_id
                return report

        validator = FakeValidator()
        with patch.object(self.module, "_capability_validator", return_value=validator):
            response = self.client.post(
                "/v1/capabilities/camera_inventory/validate",
                headers={"Authorization": "Bearer test-secret"},
                json={},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "status": "validated",
            "report_hash": "a" * 64,
            "passed": False,
            "change_kind": "selector_tier",
            "from_tier": "openclaw_only",
            "target_tier": "4b_eligible",
            "gates": {
                "replay_4b": True,
                "dangerous_negatives_4b": False,
                "shadow": True,
                "security": True,
            },
        })
        self.assertEqual(validator.capability_id, "camera_inventory")
        serialized = self.module.json.dumps(response.json())
        for private in ("private-detail", "private-tool", "sample_count"):
            self.assertNotIn(private, serialized)

    def test_capability_promotion_requires_server_report_hash_and_supports_rollback(self):
        active = self.module.capability_registry.active("camera_inventory")
        candidate = self.module.json.loads(self.module.json.dumps(active))
        candidate["version"] = 2
        candidate["status"] = "proposed"
        candidate["routing"]["examples"].append("摄像头总共有多少")
        self.module.capability_registry.propose(candidate, ["e1"])

        legacy = self.client.post(
            "/v1/capabilities/camera_inventory/promote",
            headers={"Authorization": "Bearer test-secret"},
            json={"expected_version": 1, "validation": {
                "replay_passed": True, "shadow_passed": True, "security_passed": True,
            }},
        )
        self.assertEqual(legacy.status_code, 400)
        with patch.object(self.module, "capability_replay_runner", return_value={
            "passed": True, "sample_count": 4,
            "accuracy": 1.0, "valid_json_rate": 1.0,
        }), patch.object(
            self.module.recipe_runtime, "execute", return_value="家里一共3台摄像头。",
        ):
            validated = self.client.post(
                "/v1/capabilities/camera_inventory/validate",
                headers={"Authorization": "Bearer test-secret"}, json={},
            )
        self.assertEqual(validated.status_code, 200)
        report_hash = validated.json()["report_hash"]
        self.assertNotIn("candidate_hash", validated.json())

        promoted = self.client.post(
            "/v1/capabilities/camera_inventory/promote",
            headers={"Authorization": "Bearer test-secret"},
            json={"expected_version": 1, "report_hash": report_hash},
        )
        self.assertEqual(promoted.status_code, 200)
        self.assertEqual(promoted.json()["active"]["version"], 2)
        rolled_back = self.client.post(
            "/v1/capabilities/camera_inventory/rollback",
            headers={"Authorization": "Bearer test-secret"}, json={},
        )
        self.assertEqual(rolled_back.status_code, 200)
        self.assertEqual(rolled_back.json()["active"]["version"], 1)

    def test_capability_replay_uses_dedicated_router_and_requires_exact_triple(self):
        bundle = self.module.capability_registry.active("camera_inventory")
        responses = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return self.module.json.dumps({"response": self.module.json.dumps({
                    "intent": "home", "operation": "query",
                    "capability": "none", "confidence": 0.99,
                })}).encode()

        def fake_urlopen(request, timeout):
            responses.append((request.full_url, timeout))
            fake = FakeResponse()
            fake.module = self.module
            return fake

        with patch.object(self.module.urllib.request, "urlopen", side_effect=fake_urlopen):
            metrics = self.module.capability_replay_runner(bundle)

        self.assertTrue(responses)
        self.assertTrue(all(url == "http://127.0.0.1:11435/api/generate" for url, _ in responses))
        self.assertFalse(metrics["passed"])
        self.assertEqual(metrics["valid_json_rate"], 1.0)
        self.assertEqual(metrics["accuracy"], 0.0)

    def test_recording_stream_persists_only_real_tool_stage_events(self):
        decision = self.module.RouteDecision(
            self.module.Route.HOME, "query", "semantic_home", "read_only", 1.0,
        )
        base = {
            "id": "trace-test", "object": "chat.completion.chunk",
            "created": 0, "model": "jarvis",
        }

        async def source():
            yield self.module._stage_event(
                base, phase="tool", producer="openclaw", status="started",
                data={"tool": "external_home-cli", "arguments": {"secret": "no"}},
            )
            yield self.module._sse_event(base, {"content": "完成"})
            yield self.module._sse_event(base, {}, "stop")
            yield "data: [DONE]\n\n"

        async def collect():
            return [event async for event in self.module._recording_stream(
                source(), request="查询设备", decision=decision,
                session_id="tool-trace-session",
            )]

        asyncio.run(collect())
        turn = self.module.feedback_ledger.last_turn()
        self.assertEqual(turn["tool_trace"], [{
            "provider": "openclaw", "tool": "external_home-cli", "status": "started",
        }])
        self.assertNotIn("secret", self.module.json.dumps(turn))

    def test_capability_api_exposes_only_safe_active_summary(self):
        response = self.client.get(
            "/v1/capabilities", headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(response.status_code, 200)
        active = response.json()["active"][0]
        self.assertEqual(set(active), {
            "id", "version", "status", "active_in_production",
            "planned_executor", "execution_class", "producer", "fallback", "risk",
            "selector_tier", "selector_description", "domain_tags",
            "execution_kind",
        })
        def keys(value):
            if isinstance(value, dict):
                return set(value) | set().union(*(keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(keys(item) for item in value)) if value else set()
            return set()

        private = {
            "routing", "examples", "recipe", "permissions", "allowed_tools",
            "forbidden_tools", "delivery", "review", "evidence", "prompt",
            "positive_examples", "dangerous_negatives", "min_confidence",
            "arguments", "command", "result", "secret",
        }
        self.assertTrue(private.isdisjoint(keys(response.json())))

    def test_openclaw_item_event_maps_to_safe_tool_stage_only(self):
        started = self.module._openclaw_tool_stage({
            "stream": "item", "data": {
                "name": "exec", "phase": "start", "status": "running",
                "meta": {"command": "secret"}, "title": "private title",
            },
        })
        completed = self.module._openclaw_tool_stage({
            "stream": "item", "data": {
                "name": "exec", "phase": "end", "status": "completed",
                "summary": "private result",
            },
        })
        ignored = self.module._openclaw_tool_stage({
            "stream": "command_output", "data": {
                "name": "exec", "phase": "end", "output": "private output",
            },
        })

        self.assertEqual(started, {"tool": "exec", "status": "started"})
        self.assertEqual(completed, {"tool": "exec", "status": "completed"})
        self.assertIsNone(ignored)
        self.assertNotIn("secret", self.module.json.dumps(started))
        self.assertNotIn("private", self.module.json.dumps(completed))

    def test_non_stream_openclaw_collects_safe_tool_trace(self):
        base = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "jarvis"}

        async def source(*_):
            yield self.module._stage_event(
                base, phase="tool", producer="openclaw", status="completed",
                data={"tool": "exec", "output": "private"},
            )
            yield self.module._sse_event(base, {"content": "完成"})
            yield "data: [DONE]\n\n"

        trace = []
        with patch.object(self.module, "_openclaw_ws_stream", source):
            answer = asyncio.run(self.module.run_openclaw(
                {"model": "jarvis"}, self.module.Route.HOME, tool_trace=trace,
            ))
        self.assertEqual(answer, "完成")
        self.assertEqual(trace, [{
            "provider": "openclaw", "tool": "exec", "status": "completed",
        }])
        self.assertNotIn("private", self.module.json.dumps(trace))

    def test_web_query_requires_observed_completed_tool(self):
        self.assertFalse(self.module._web_query_has_observed_tool(
            self.module.Route.WEB_QUERY, [],
        ))
        self.assertFalse(self.module._web_query_has_observed_tool(
            self.module.Route.WEB_QUERY,
            [{"provider": "openclaw", "tool": "exec", "status": "started"}],
        ))
        self.assertTrue(self.module._web_query_has_observed_tool(
            self.module.Route.WEB_QUERY,
            [{"provider": "openclaw", "tool": "exec", "status": "completed"}],
        ))
        self.assertTrue(self.module._web_query_has_observed_tool(
            self.module.Route.LOCAL_CHAT, [],
        ))

    def test_non_stream_web_query_rejects_answer_without_tool_evidence(self):
        base = {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "jarvis"}

        async def source(*_):
            yield self.module._sse_event(base, {"content": "实时天气是晴天"})
            yield "data: [DONE]\n\n"

        with patch.object(self.module, "_openclaw_ws_stream", source):
            with self.assertRaisesRegex(RuntimeError, "web_query_missing_tool_evidence"):
                asyncio.run(self.module.run_openclaw(
                    {"model": "jarvis"}, self.module.Route.WEB_QUERY,
                ))

    def test_explicit_route_review_rejects_unknown_turn(self):
        response = self.client.post(
            "/v1/route-reviews",
            headers={"Authorization": "Bearer test-secret"},
            json={"turn_id": "missing"},
        )
        self.assertEqual(response.status_code, 404)

    def test_turn_history_is_authenticated_and_returns_safe_stable_turns(self):
        turn = self.module.feedback_ledger.save_turn({
            "request": "讲个笑话", "route": "local_chat", "intent": "chat",
            "executor": "local_9b", "answer": "一个笑话。", "success": True,
        })
        self.assertEqual(self.client.get("/v1/turns").status_code, 401)
        response = self.client.get(
            "/v1/turns?limit=10",
            headers={"Authorization": "Bearer test-secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["turns"][0]["turn_id"], turn["turn_id"])
        self.assertEqual(set(response.json()["turns"][0]), {
            "turn_id", "session_id", "time", "request", "route", "intent",
            "executor", "producer", "capability", "capability_version",
            "escalation_reason", "tool_trace", "answer", "success",
            "arbitration_id", "level1", "level2", "decision_tier",
            "execution_class", "tool_class", "selector_tier", "risk_class",
        })
        serialized = self.module.json.dumps(response.json()["turns"][0])
        for private in ("prompt", "arguments", "command", "result", "secret"):
            self.assertNotIn(private, serialized)

    def test_model_unavailable_stream_is_recorded_for_history_and_review(self):
        decision = RouteDecision(self.module.Route.LOCAL_CHAT, "chat", "model_unavailable")
        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={"stream": True, "messages": [{"role": "user", "content": "含糊请求"}]},
            )
        events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        metadata = self.module.json.loads(events[-2])
        turn_id = metadata["jarvis"]["turn_id"]
        turn = self.module.feedback_ledger.get_turn(turn_id)
        self.assertEqual(turn["request"], "含糊请求")
        self.assertEqual(turn["route"], "local_chat")
        self.assertFalse(turn["success"])
        self.assertIn("没判断清楚", turn["answer"])

    def test_stream_request_returns_openai_sse_events(self):
        result = {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": "home",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "乌龟正在晒背。"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
        decision = RouteDecision(self.module.Route.CAMERA, "camera_recent", "semantic_camera_recent", "read_only")
        with (
            patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)),
            patch.object(self.module, "run_external_home_query", return_value="乌龟正在晒背。"),
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "stream": True,
                    "messages": [{"role": "user", "content": "乌龟在干嘛"}],
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = [line for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-1], "data: [DONE]")
        stage = self.module.json.loads(events[0][6:])
        self.assertEqual(stage["choices"][0]["delta"], {})
        self.assertFalse(stage["jarvis"]["speak"])
        self.assertEqual(stage["jarvis"]["producer"], "router_0_8b")
        machine = [
            self.module.json.loads(event[6:])["jarvis"]
            for event in events[:-1]
            if '"event":"stage"' in event
        ]
        self.assertEqual(
            [(item["phase"], item["producer"], item["status"]) for item in machine],
            [
                ("arbitration", "router_0_8b", "started"),
                ("arbitration", "router_0_8b", "completed"),
                ("execution", "external_home", "started"),
                ("execution", "external_home", "completed"),
            ],
        )
        self.assertTrue(all(item["speak"] is False for item in machine))
        self.assertIn('"delta":{"content":"乌龟正在晒背。"}', "\n".join(events))
        self.assertTrue(any('"finish_reason":"stop"' in event for event in events))
        self.assertNotIn('"message"', events[1])

    def test_stream_returns_recorded_turn_id_before_done_without_tts_content(self):
        decision = RouteDecision(self.module.Route.CAMERA, "camera_recent", "semantic_camera_recent", "read_only")
        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)), patch.object(self.module, "run_external_home_query", return_value="乌龟正在晒背。"):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={"stream": True, "messages": [{"role": "user", "content": "乌龟在干嘛"}]},
            )
        events = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        metadata = self.module.json.loads(events[-2])
        self.assertEqual(events[-1], "[DONE]")
        self.assertTrue(metadata["jarvis"]["turn_id"])
        self.assertEqual(metadata["jarvis"]["stage"], "recorded")
        self.assertNotIn("content", metadata["choices"][0]["delta"])
        self.assertEqual(
            self.module.feedback_ledger.get_turn(metadata["jarvis"]["turn_id"])["request"],
            "乌龟在干嘛",
        )

    def test_slow_openclaw_query_emits_route_specific_ack_immediately(self):
        decision = RouteDecision(self.module.Route.HOME, "query", "semantic_home", "read_only")

        async def slow_openclaw(_body, _model, _route):
            await self.module.asyncio.sleep(0.05)
            yield self.module._sse_event({
                "id": "chatcmpl-test", "object": "chat.completion.chunk",
                "created": 0, "model": "home",
            }, {"content": "宠物龟最近在晒背。"})
            yield "data: [DONE]\n\n"

        with (
            patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)),
            patch.object(self.module, "_openclaw_ws_stream", side_effect=slow_openclaw),
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={"stream": True, "session_id": "ack-test", "messages": [{"role": "user", "content": "宠物龟在干嘛"}]},
            )
        spoken = []
        for line in response.text.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            frame = self.module.json.loads(line[6:])
            text = ((frame.get("choices") or [{}])[0].get("delta") or {}).get("content")
            if text:
                spoken.append(text)
        self.assertEqual(spoken[0], "好的主人，我查一下。")
        self.assertEqual(spoken[-1], "宠物龟最近在晒背。")

    def test_slow_visual_stream_emits_ack_before_completion(self):
        def slow_complete(_body):
            time.sleep(0.5)
            return {
                "id": "chatcmpl-slow",
                "object": "chat.completion",
                "created": 1,
                "model": "home",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "乌龟正在休息。"}, "finish_reason": "stop"}],
            }

        def slow_external_home(_text):
            result = slow_complete({})
            return result["choices"][0]["message"]["content"]

        decision = RouteDecision(self.module.Route.CAMERA, "camera_recent", "semantic_camera_recent", "read_only")
        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)), patch.object(self.module, "run_external_home_query", side_effect=slow_external_home):
            with self.client.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={"stream": True, "messages": [{"role": "user", "content": "贾维斯，我的乌龟在干嘛"}]},
            ) as response:
                lines = (line for line in response.iter_lines() if line.startswith("data: "))
                first = next(lines)
                rest = list(lines)

        self.assertIn('"producer":"router_0_8b"', first)
        self.assertIn("好的主人，我看一下。", "\n".join(rest))
        self.assertIn('"delta":{}', rest[0])
        self.assertIn('"delta":{"content":"乌龟正在休息。"}', "\n".join(rest))
        self.assertEqual(rest[-1], "data: [DONE]")


    def test_slow_local_chat_emits_ack_before_9b_answer(self):
        decision = RouteDecision(self.module.Route.LOCAL_CHAT, "chat", "semantic_chat", "read_only")

        async def slow_local(_body):
            await self.module.asyncio.sleep(0.05)
            yield self.module._sse_event({
                "id": "chatcmpl-test", "object": "chat.completion.chunk",
                "created": 0, "model": "qwen3.5:9b",
            }, {"content": "量子纠缠是粒子间的关联。"})
            yield "data: [DONE]\n\n"

        with (
            patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)),
            patch.object(self.module.local_chat, "stream", side_effect=slow_local),
        ):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={"stream": True, "session_id": "local-ack-test", "messages": [{"role": "user", "content": "解释量子纠缠"}]},
            )
        spoken = []
        for line in response.text.splitlines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            frame = self.module.json.loads(line[6:])
            text = ((frame.get("choices") or [{}])[0].get("delta") or {}).get("content")
            if text:
                spoken.append(text)
        self.assertEqual(spoken[0], "好的主人，我想一下。")
        self.assertEqual(spoken[-1], "量子纠缠是粒子间的关联。")

    def test_task_request_uses_openclaw_task_path(self):
        body = {
            "stream": False,
            "messages": [{"role": "user", "content": "每天晚上九点提醒我看看宠物仓鼠的饮水器"}],
        }
        decision = RouteDecision(self.module.Route.TASK, "create", "semantic_task", "task_mutation")
        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)), patch.object(self.module, "run_openclaw", AsyncMock(return_value="任务已经创建。")) as run:
            result = self.module.asyncio.run(self.module._complete_chat(body))
        self.assertEqual(result["choices"][0]["message"]["content"], "任务已经创建。")
        run.assert_awaited_once_with(
            body, self.module.Route.TASK, tool_trace=ANY,
        )

    def test_api_uses_semantic_router_decision(self):
        body = {
            "stream": False,
            "messages": [{"role": "user", "content": "以后照这个办"}],
        }
        semantic_decision = RouteDecision(
            self.module.Route.TASK, "create", "semantic_task", "task_mutation"
        )
        with (
            patch.object(
                self.module.intent_router,
                "decide",
                AsyncMock(return_value=semantic_decision),
            ) as decide,
            patch.object(
                self.module,
                "run_openclaw",
                AsyncMock(return_value="任务已经创建。"),
            ) as run,
        ):
            result = self.module.asyncio.run(self.module._complete_chat(body))
        self.assertEqual(result["choices"][0]["message"]["content"], "任务已经创建。")
        decide.assert_awaited_once_with("以后照这个办")
        run.assert_awaited_once_with(
            body, self.module.Route.TASK, tool_trace=ANY,
        )

    def test_voice_cleaner_keeps_only_final_answer_after_agent_planning(self):
        raw = (
            "返回结果是0个任务。说明当前没有任何家庭任务。"
            "我需要用简短自然中文回答用户。你当前没有任何家庭任务。"
        )
        self.assertEqual(
            self.module._clean_voice_agent_result(raw),
            "你当前没有任何家庭任务。",
        )

    def test_task_agent_prompt_allows_external_home_task_management(self):
        prompt = self.module._agent_extra_prompt(self.module.Route.TASK)
        self.assertIn("家庭任务", prompt)
        self.assertIn("external_home-create-task", prompt)
        self.assertNotIn("只处理明确的家庭设备查询或控制", prompt)

    def test_home_agent_prompt_remains_device_scoped(self):
        prompt = self.module._agent_extra_prompt(self.module.Route.HOME)
        self.assertIn("只处理明确的家庭设备查询或控制", prompt)
        self.assertNotIn("external_home-create-task", prompt)

    def test_task_progress_phrases_describe_real_task_work(self):
        self.assertEqual(
            self.module._progress_phrases(RouteDecision(self.module.Route.TASK, "create", "semantic_task")),
            (
                "好的主人，我来处理这个任务。",
                "OpenClaw正在规划并核验任务设置。",
                "任务还在处理，你再等我一下。",
            ),
        )

    def test_task_list_progress_does_not_claim_creation(self):
        self.assertEqual(
            self.module._progress_phrases(RouteDecision(self.module.Route.TASK, "list", "semantic_task")),
            (
                "好的主人，我查一下当前任务。",
                "我正在读取任务列表。",
                "任务列表还在读取，你再等我一下。",
            ),
        )

    def test_failed_task_stream_never_claims_success(self):
        async def failed_stream(_body, _model):
            raise RuntimeError("agent unavailable")
            yield  # pragma: no cover

        decision = RouteDecision(self.module.Route.TASK, "create", "semantic_task", "task_mutation")
        with patch.object(self.module.intent_router, "decide", AsyncMock(return_value=decision)), patch.object(self.module, "_openclaw_ws_stream", side_effect=failed_stream):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "stream": True,
                    "messages": [{"role": "user", "content": "每天晚上九点提醒我喝水"}],
                },
            )
        self.assertIn("任务没有创建成功", response.text)
        self.assertNotIn("已经创建", response.text)

    def test_non_stream_request_remains_json(self):
        result = {"choices": [{"message": {"role": "assistant", "content": "正常回答"}}]}
        with patch.object(self.module, "_complete_chat", AsyncMock(return_value=result)):
            response = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer test-secret"},
                json={
                    "stream": False,
                    "messages": [{"role": "user", "content": "请回答"}],
                },
            )
        self.assertTrue(response.headers["content-type"].startswith("application/json"))
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "正常回答")


if __name__ == "__main__":
    unittest.main()
