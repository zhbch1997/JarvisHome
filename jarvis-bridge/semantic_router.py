"""Bounded local semantic arbitration with deterministic fail-closed fallback."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from capability_registry import CapabilityRegistry
from router import Route, RouteDecision, Router


_ALLOWED_INTENTS = {
    "task",
    "camera_recent",
    "camera_live",
    "home",
    "web_query",
    "chat",
}

_SYSTEM_PROMPT = """你是家庭语音请求分类器。只能输出JSON，字段intent、operation、capability和confidence。intent仅限task、camera_recent、camera_live、home、web_query、chat。根据请求的目标分类，不要根据单个词猜测。
对比示例：
“每天九点提醒我看饮水器”=>task
“宠物仓鼠在干嘛”=>camera_recent
“现在实时看看宠物仓鼠”=>camera_live
“打开示例房间的灯”=>home
“今天股票行情怎么样”=>web_query
“为什么灯会发光”=>chat
“家里没人时窗户开着就通知我”=>task
“看看乌龟最近有没有晒背”=>camera_recent
“宠物龟在干嘛”=>camera_recent
“乌龟现在在做什么”=>camera_recent
“宠物仓鼠出来了吗”=>camera_recent
“重新打开摄像头看乌龟”=>camera_live
“家里有几台摄像头”=>home/query/camera_inventory
未来提醒、定时、条件或持续观察归task；宠物当前活动问法默认读取近期感知记录，归camera_recent，不要归home；只有明确要求重新打开或实时查看摄像头才归camera_live；立即智能设备查询控制归home；需要外部实时信息或联网检索的股票、天气、新闻、赛事、汇率等归web_query；知识解释和闲聊归chat。
operation只能是create、list、update、delete、query、action、chat。capability目前仅限camera_inventory或none；只有询问家中摄像头数量、清单或有哪些摄像头时输出camera_inventory，其他请求一律输出none。
task按用户目标输出create/list/update/delete/query；home输出query或action；web_query输出query；camera输出query；chat输出chat。
固定输出格式：{"intent":"task","operation":"create","capability":"none","confidence":0.99}"""

_EXAMPLE_ROUTES = {"task", "camera", "home", "web_query", "local_chat"}
_EXAMPLE_INTENTS = {
    "create", "list", "update", "delete", "query", "action", "chat",
    "camera_recent", "camera_live",
}
_EXAMPLE_EXECUTORS = {"external_home", "local_9b", "openclaw"}
_DEFAULT_EXAMPLES = Path(
    os.getenv("JARVIS_ROUTER_EXAMPLES", "./state/route-feedback/router-examples.json")
)


def build_router_prompt(
    path: Path = _DEFAULT_EXAMPLES,
    *,
    capability_registry: CapabilityRegistry | None = None,
) -> str:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = []
    examples: list[dict[str, str]] = []
    required = {"request", "route", "intent", "executor"}
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or set(item) != required:
            continue
        request = str(item["request"])
        route = str(item["route"])
        intent = str(item["intent"])
        executor = str(item["executor"])
        if not request or len(request) > 500:
            continue
        if route not in _EXAMPLE_ROUTES or intent not in _EXAMPLE_INTENTS:
            continue
        if executor not in _EXAMPLE_EXECUTORS:
            continue
        model_intent = (
            intent if intent in {"camera_recent", "camera_live"}
            else "chat" if route == "local_chat"
            else route
        )
        operation = intent if intent in {"create", "list", "update", "delete", "query", "action", "chat"} else "query"
        examples.append({"input": request, "output": {"intent": model_intent, "operation": operation, "confidence": 0.99}})
    capability_pairs: list[str] = []
    if capability_registry is not None:
        for bundle in capability_registry.list_active():
            routing = bundle["routing"]
            capability_pairs.extend(
                f'“{text}”=>{routing["route"]}/{routing["operation"]}/{bundle["id"]}'
                for text in routing["examples"][-20:]
            )
    legacy_pairs = [
        f'“{item["input"]}”=>{item["output"]["intent"]}/{item["output"]["operation"]}'
        for item in examples[-20:]
    ]
    pairs = (capability_pairs[-40:] + legacy_pairs)[-60:]
    if not pairs:
        return _SYSTEM_PROMPT
    return _SYSTEM_PROMPT + "\n已激活能力包与OpenClaw复审的补充对比示例：\n" + "\n".join(pairs)


_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": sorted(_ALLOWED_INTENTS)},
        "operation": {"type": "string", "enum": ["create", "list", "update", "delete", "query", "action", "chat"]},
        "capability": {"type": "string", "enum": ["camera_inventory", "none"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["intent", "operation", "capability", "confidence"],
    "additionalProperties": False,
}


@dataclass
class OllamaIntentClassifier:
    url: str = os.getenv("JARVIS_ROUTER_URL", "http://127.0.0.1:11435/api/generate")
    model: str = os.getenv("JARVIS_ROUTER_MODEL", "qwen35-router:0.8b")
    timeout: float = float(os.getenv("JARVIS_ROUTER_TIMEOUT", "5"))
    capability_registry: CapabilityRegistry | None = None

    def system_prompt(self) -> str:
        return build_router_prompt(capability_registry=self.capability_registry)

    async def classify(self, text: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "system": self.system_prompt(),
            "prompt": text,
            "stream": False,
            "format": _OUTPUT_SCHEMA,
            "think": False,
            "keep_alive": -1,
            "options": {
                "temperature": 0,
                "num_ctx": 2048,
                "num_predict": 40,
            },
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.url, json=payload)
            response.raise_for_status()
        outer = response.json()
        parsed = json.loads(str(outer.get("response") or ""))
        if not isinstance(parsed, dict):
            raise ValueError("semantic router returned a non-object")
        return parsed


class SemanticRouter:
    def __init__(
        self,
        fallback: Router,
        classifier: Any,
        *,
        enabled: bool = False,
        min_confidence: float = 0.8,
    ) -> None:
        self.fallback = fallback
        self.classifier = classifier
        self.enabled = enabled
        self.min_confidence = min_confidence

    async def decide(self, text: str) -> RouteDecision:
        deterministic = self.fallback.decide(text)
        # Only native speaker commands bypass the learning router. All normal
        # task/camera/home/chat traffic is arbitrated by the small model; the
        # deterministic router is strictly an availability fallback.
        if deterministic.route is Route.NATIVE or not self.enabled:
            return deterministic

        try:
            result = await self.classifier.classify(text)
            intent = str(result.get("intent") or "")
            operation = str(result.get("operation") or "")
            capability = str(result.get("capability") or "none")
            confidence = float(result.get("confidence") or 0)
        except (asyncio.TimeoutError, httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError):
            return deterministic

        if intent not in _ALLOWED_INTENTS or confidence < self.min_confidence:
            return deterministic
        if capability not in {"camera_inventory", "none"}:
            return deterministic
        if capability == "camera_inventory" and not (intent == "home" and operation == "query"):
            return deterministic
        if intent == "task":
            task_intent = operation if operation in {"create", "list", "update", "delete", "query"} else "query"
            risk = "read_only" if task_intent in {"list", "query"} else "task_mutation"
            return RouteDecision(Route.TASK, task_intent, "semantic_task", risk, confidence)
        if intent in {"camera_recent", "camera_live"}:
            return RouteDecision(Route.CAMERA, intent, f"semantic_{intent}", "read_only", confidence)
        if intent == "home":
            home_intent = "action" if operation == "action" else "query"
            return RouteDecision(Route.HOME, home_intent, "semantic_home", "device_action" if home_intent == "action" else "read_only", confidence, capability)
        if intent == "web_query":
            return RouteDecision(Route.WEB_QUERY, "query", "semantic_web_query", "read_only", confidence)
        return RouteDecision(Route.LOCAL_CHAT, "chat", "semantic_chat", "none", confidence)
