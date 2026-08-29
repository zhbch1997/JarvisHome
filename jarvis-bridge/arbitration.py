"""Hierarchical, fail-safe arbitration envelopes for Jarvis."""
from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from capability_registry import CapabilityRegistry


_LEVEL1_DECISIONS = {"chat", "quick_tool", "handoff"}
_HANDOFFS = {"none", "lookup", "analyze", "action", "general"}
_ROUTINE_SCENE_PATTERN = re.compile(r"该睡觉了|准备睡觉|主卧[-的]?起床|打开电视|关闭电视|客厅新风|叫醒家人")


def _normalized_exact_text(text: str) -> str:
    return re.sub(r"[\s，。！？、,.!?；;：:]+", "", str(text or "")).lower()


def _exact_level1_quick_tool(
    text: str, registry: CapabilityRegistry,
) -> dict[str, Any] | None:
    normalized = _normalized_exact_text(text)
    if not normalized:
        return None
    matches = []
    for bundle in registry.list_selectable("0.8b"):
        examples = bundle["selector"]["positive_examples"]
        if normalized in {_normalized_exact_text(item) for item in examples}:
            matches.append(bundle["id"])
    if len(matches) != 1:
        return None
    return {
        "decision": "quick_tool", "quick_tool_id": matches[0],
        "handoff": "lookup", "confidence": 1.0,
    }


def level1_output_schema(registry: CapabilityRegistry) -> dict[str, Any]:
    tool_ids = sorted(bundle["id"] for bundle in registry.list_selectable("0.8b"))
    required = ["decision", "quick_tool_id", "handoff", "confidence"]
    confidence = {"type": "number", "minimum": 0, "maximum": 1}

    def branch(decision: str, tool: dict[str, Any], handoff: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "decision": {"const": decision},
                "quick_tool_id": tool,
                "handoff": handoff,
                "confidence": confidence,
            },
            "required": required,
            "additionalProperties": False,
        }

    return {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": sorted(_LEVEL1_DECISIONS)},
            "quick_tool_id": {"type": "string", "enum": tool_ids + ["none"]},
            "handoff": {"type": "string", "enum": sorted(_HANDOFFS)},
            "confidence": confidence,
        },
        "required": required,
        "additionalProperties": False,
        "oneOf": [
            branch("chat", {"const": "none"}, {"const": "none"}),
            branch(
                "quick_tool", {"enum": tool_ids},
                {"enum": ["lookup", "analyze"]},
            ),
            branch(
                "handoff", {"const": "none"},
                {"enum": ["lookup", "analyze", "action", "general"]},
            ),
        ],
    }


def _level1_active_pairs(path: Path, allowed_tool_ids: set[str]) -> list[str]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    pairs = []
    required = {"request", "decision", "quick_tool_id", "handoff", "evidence_ids"}
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or set(item) != required:
            continue
        request = str(item["request"]).strip()
        decision = str(item["decision"])
        tool_id = str(item["quick_tool_id"])
        handoff = str(item["handoff"])
        if (
            not request or len(request) > 500
            or decision not in _LEVEL1_DECISIONS or handoff not in _HANDOFFS
            or not isinstance(item["evidence_ids"], list) or not item["evidence_ids"]
        ):
            continue
        if decision == "chat" and not (tool_id == "none" and handoff == "none"):
            continue
        if decision == "quick_tool" and not (tool_id != "none" and handoff in {"lookup", "analyze"}):
            continue
        if decision == "quick_tool" and tool_id not in allowed_tool_ids:
            continue
        if decision == "handoff" and not (tool_id == "none" and handoff != "none"):
            continue
        pairs.append(f'“{request}”=>{decision}/{tool_id}/{handoff}')
    return pairs[-20:]


def build_level1_prompt(
    registry: CapabilityRegistry, *, examples_path: Path | None = None,
) -> str:
    tools = registry.list_selectable("0.8b")
    catalog = []
    for bundle in tools:
        selector = bundle["selector"]
        positives = "；".join(selector["positive_examples"][-10:])
        negatives = "；".join(selector["dangerous_negatives"][-10:]) or "无"
        catalog.append(
            f'- {bundle["id"]}：{selector["description"]}\n'
            f'  正例：{positives}\n  危险负例：{negatives}'
        )
    tool_rules = "\n".join(catalog) if catalog else "（当前没有0.8B可直选的Quick Tool）"
    active_path = examples_path or Path(os.getenv(
        "JARVIS_LEVEL1_EXAMPLES",
        "./state/route-feedback/level1-active-examples.json",
    ))
    active_pairs = _level1_active_pairs(active_path, {bundle["id"] for bundle in tools})
    learned_rules = (
        "\n已通过每日回放门禁的显式复审示例：\n" + "\n".join(active_pairs)
        if active_pairs else ""
    )
    return f"""你是Jarvis一级仲裁器，只输出JSON。
只做三选一：
- chat：无需实时事实、无需工具、无需执行的普通知识与闲聊。
- quick_tool：仅当请求精确匹配下方0.8B可直选目录时使用。
- handoff：其他全部请求，包括实时事实、联网、设备、任务、购物、文件、分析、写操作和不确定请求。
handoff只选择过渡语类型：lookup=查询，analyze=分析，action=写入或控制，general=无法可靠细分。它不授予工具或执行权限。
组合必须满足：chat→none/none；quick_tool→精确ID且handoff只能lookup或analyze；handoff→none且handoff非none。
特别注意：chat 的 handoff 必须是 none，不能因为回答问题而写成lookup；只有需要外部查询或工具时才用handoff/lookup。
闲聊示例：“讲个简短的笑话”→{{"decision":"chat","quick_tool_id":"none","handoff":"none","confidence":0.99}}
实时查询示例：“查询今天北京天气”→{{"decision":"handoff","quick_tool_id":"none","handoff":"lookup","confidence":0.99}}
家庭实时事实示例：“家里有几台摄像头”→{{"decision":"handoff","quick_tool_id":"none","handoff":"lookup","confidence":0.99}}
宠物当前位置、活动或状态通常属于家庭实时事实；但若下方目录有与命名宠物精确对应的近期记录Quick Tool，就应直接选择该Quick Tool。用户明确要求“现在打开摄像头/实时看看”时不得用近期记录，必须handoff/lookup。
示例：“看看宠物仓鼠在干嘛”且目录存在hamster_recent_activity→{{"decision":"quick_tool","quick_tool_id":"hamster_recent_activity","handoff":"lookup","confidence":0.99}}
示例：“现在打开摄像头看看宠物仓鼠”→{{"decision":"handoff","quick_tool_id":"none","handoff":"lookup","confidence":0.99}}
非法组合：{{"decision":"chat","quick_tool_id":"none","handoff":"lookup","confidence":0.99}}
低置信度或边界不清时输出handoff/none/general。
0.8B可直选Quick Tool目录：
{tool_rules}
最终对照：
- “宠物龟在干嘛”→quick_tool/turtle_recent_activity/lookup。
- “宠物仓鼠在干嘛”→quick_tool/hamster_recent_activity/lookup。
- “现在打开摄像头看看宠物龟或宠物仓鼠”→handoff/none/lookup。
- “乌龟或仓鼠为什么有这种习性”→chat/none/none。
最终检查：chat只允许不依赖这个家庭当前情况也能恒真回答的问题。家庭现场问题只有在精确匹配目录中的只读Quick Tool时才能quick_tool，否则handoff/lookup；明确要求实时查看时始终handoff。一般原理、知识和习性仍用chat。
{learned_rules}
"""


def level2_output_schema(registry: CapabilityRegistry) -> dict[str, Any]:
    tool_ids = sorted(bundle["id"] for bundle in registry.list_selectable("4b"))
    return {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["openclaw", "quick_tool"]},
            "quick_tool_id": {"type": "string", "enum": tool_ids + ["none"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "required": ["decision", "quick_tool_id", "confidence"],
        "additionalProperties": False,
    }


def build_level2_prompt(
    registry: CapabilityRegistry, envelope: "ArbitrationEnvelope",
) -> str:
    tools = registry.list_selectable("4b")
    catalog = []
    for bundle in tools:
        selector = bundle["selector"]
        positives = "；".join(selector["positive_examples"][-10:])
        negatives = "；".join(selector["dangerous_negatives"][-10:]) or "无"
        catalog.append(
            f'- {bundle["id"]}：{selector["description"]}\n'
            f'  正例：{positives}\n  危险负例：{negatives}'
        )
    tool_rules = "\n".join(catalog) if catalog else "（当前没有4B可选择的Quick Tool）"
    return f"""你是Jarvis二级仲裁器，只输出JSON。
父级仲裁ID={envelope.arbitration_id}
父级结果=decision={envelope.level1.decision},handoff={envelope.level1.handoff}
你的职责仅是：从下方4B可选目录中选择一个精确Quick Tool，或交给OpenClaw。
Quick Tool只用于完整、唯一匹配的已注册确定性场景；普通家庭设备控制仅可选择目录中明确标记device_action的能力。开放任务、联网、未知任务、边界不清和多步规划一律openclaw。
单台已授权普通米家设备的开关、温度、亮度等直接控制，即使用户使用完整设备名、型号名或房间+设备名，也属于device_action；不要仅因名称很长就判为未知任务。门锁、摄像头、燃气、烟雾报警、支付、购买、删除自动化和多步开放规划不属于普通设备控制，必须openclaw。
摄像头数量、名称或清单查询应选择目录里的camera_inventory_quick；打开摄像头、查看画面或查询宠物活动不是摄像头清单，必须按其他匹配能力选择或openclaw。
示例：“打开示例房间的米家智能显示器挂灯1S”→{{"decision":"quick_tool","quick_tool_id":"home_device_action","confidence":0.99}}
示例：“把主卧空调调到二十六度”→{{"decision":"quick_tool","quick_tool_id":"home_device_action","confidence":0.99}}
示例：“把门锁打开”→{{"decision":"openclaw","quick_tool_id":"none","confidence":0.99}}
示例：“家里有几台摄像头”→{{"decision":"quick_tool","quick_tool_id":"camera_inventory_quick","confidence":0.99}}
示例：“现在打开摄像头看看”→{{"decision":"openclaw","quick_tool_id":"none","confidence":0.99}}
不得输出工具参数、执行器、权限或Recipe。选择openclaw时quick_tool_id必须是none。
4B可选Quick Tool目录：
{tool_rules}
"""


@dataclass
class OllamaLevel1Classifier:
    registry: CapabilityRegistry
    url: str = os.getenv("JARVIS_ROUTER_URL", "http://127.0.0.1:11435/api/generate")
    model: str = os.getenv("JARVIS_ROUTER_MODEL", "qwen35-router:0.8b")
    timeout: float = float(os.getenv("JARVIS_ROUTER_TIMEOUT", "5"))
    transport: Any = None

    async def classify(self, text: str) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "system": build_level1_prompt(self.registry),
            "prompt": text,
            "stream": False,
            "format": level1_output_schema(self.registry),
            "think": False,
            "keep_alive": -1,
            "options": {
                "temperature": 0,
                "num_ctx": 2048,
                "num_predict": 40,
            },
        }
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport,
        ) as client:
            response = await client.post(self.url, json=payload)
            response.raise_for_status()
        outer = response.json()
        parsed = json.loads(str(outer.get("response") or ""))
        if not isinstance(parsed, dict):
            raise ValueError("level1 classifier returned a non-object")
        return parsed


@dataclass
class OllamaLevel2Classifier:
    registry: CapabilityRegistry
    url: str = os.getenv("JARVIS_LEVEL2_URL", "http://127.0.0.1:11434/api/generate")
    model: str = os.getenv("JARVIS_LEVEL2_MODEL", "qwen35-4b-16k:latest")
    timeout: float = float(os.getenv("JARVIS_LEVEL2_TIMEOUT", "10"))
    transport: Any = None

    async def classify(
        self, text: str, envelope: "ArbitrationEnvelope",
    ) -> dict[str, Any]:
        payload = {
            "model": self.model,
            "system": build_level2_prompt(self.registry, envelope),
            "prompt": text,
            "stream": False,
            "format": level2_output_schema(self.registry),
            "think": False,
            "keep_alive": -1,
            "options": {
                "temperature": 0,
                "seed": 0,
                "num_ctx": 2048,
                "num_predict": 30,
            },
        }
        async with httpx.AsyncClient(
            timeout=self.timeout, transport=self.transport,
        ) as client:
            response = await client.post(self.url, json=payload)
            response.raise_for_status()
        outer = response.json()
        parsed = json.loads(str(outer.get("response") or ""))
        if not isinstance(parsed, dict):
            raise ValueError("level2 classifier returned a non-object")
        return parsed


@dataclass(frozen=True)
class Level1Decision:
    decision: str
    quick_tool_id: str
    handoff: str
    confidence: float


@dataclass(frozen=True)
class Level2Decision:
    decision: str
    quick_tool_id: str
    relation: str
    confidence: float


@dataclass(frozen=True)
class ArbitrationEnvelope:
    arbitration_id: str
    level1: Level1Decision
    level2_required: bool
    level2: Level2Decision | None = None


def _safe_envelope() -> ArbitrationEnvelope:
    return ArbitrationEnvelope(
        arbitration_id=f"arb-{uuid.uuid4().hex}",
        level1=Level1Decision(
            decision="handoff", quick_tool_id="none",
            handoff="general", confidence=0.0,
        ),
        level2_required=True,
    )


class Level1Arbitrator:
    def __init__(
        self, classifier: Any, registry: CapabilityRegistry, *,
        min_confidence: float = 0.9,
    ) -> None:
        self.classifier = classifier
        self.registry = registry
        self.min_confidence = min_confidence

    async def decide(self, text: str) -> ArbitrationEnvelope:
        try:
            # A small fail-safe lexical guard prevents explicit allowlisted
            # scene actions from being terminally misclassified as local chat.
            normalized = _normalized_exact_text(text)
            if re.search(r"空调睡眠(?:24|25|26)度", normalized) or _ROUTINE_SCENE_PATTERN.search(normalized):
                return ArbitrationEnvelope(
                    f"arb-{uuid.uuid4().hex}",
                    Level1Decision("handoff", "none", "action", 1.0),
                    True,
                )
            result = _exact_level1_quick_tool(text, self.registry)
            if result is None:
                result = await self.classifier.classify(text)
            if not isinstance(result, dict) or set(result) != {
                "decision", "quick_tool_id", "handoff", "confidence",
            }:
                return _safe_envelope()
            decision = str(result["decision"])
            tool_id = str(result["quick_tool_id"])
            handoff = str(result["handoff"])
            confidence_value = result["confidence"]
            if (
                decision not in _LEVEL1_DECISIONS
                or handoff not in _HANDOFFS
                or isinstance(confidence_value, bool)
                or not isinstance(confidence_value, (int, float))
            ):
                return _safe_envelope()
            confidence = float(confidence_value)
            if not 0 <= confidence <= 1 or confidence < self.min_confidence:
                return _safe_envelope()

            if decision == "chat":
                if tool_id != "none" or handoff != "none":
                    return _safe_envelope()
                level1 = Level1Decision(decision, tool_id, handoff, confidence)
                return ArbitrationEnvelope(f"arb-{uuid.uuid4().hex}", level1, False)

            if decision == "handoff":
                if tool_id != "none" or handoff == "none":
                    return _safe_envelope()
                level1 = Level1Decision(decision, tool_id, handoff, confidence)
                return ArbitrationEnvelope(f"arb-{uuid.uuid4().hex}", level1, True)

            selectable = {
                bundle["id"]: bundle for bundle in self.registry.list_selectable("0.8b")
            }
            bundle = selectable.get(tool_id)
            if bundle is None or handoff not in {"lookup", "analyze"}:
                return _safe_envelope()
            required_confidence = max(
                self.min_confidence,
                float(bundle["selector"]["min_confidence"]["0.8b"]),
            )
            if confidence < required_confidence:
                return _safe_envelope()
            level1 = Level1Decision(decision, tool_id, handoff, confidence)
            return ArbitrationEnvelope(f"arb-{uuid.uuid4().hex}", level1, False)
        except Exception:
            return _safe_envelope()


def _escalated_level2(
    envelope: ArbitrationEnvelope, confidence: float = 0.0,
) -> ArbitrationEnvelope:
    return ArbitrationEnvelope(
        arbitration_id=envelope.arbitration_id,
        level1=envelope.level1,
        level2_required=True,
        level2=Level2Decision(
            decision="openclaw", quick_tool_id="none",
            relation="escalated", confidence=confidence,
        ),
    )


class Level2Arbitrator:
    def __init__(
        self, classifier: Any, registry: CapabilityRegistry, *,
        min_confidence: float = 0.9,
    ) -> None:
        self.classifier = classifier
        self.registry = registry
        self.min_confidence = min_confidence

    async def decide(
        self, text: str, envelope: ArbitrationEnvelope,
    ) -> ArbitrationEnvelope:
        if not envelope.level2_required or envelope.level1.decision != "handoff":
            raise ValueError("level2 requires a handoff parent")
        try:
            selectable = {
                bundle["id"]: bundle for bundle in self.registry.list_selectable("4b")
            }
            if (
                "home_scene_action" in selectable
                and re.search(r"空调睡眠(?:24|25|26)度", _normalized_exact_text(text))
            ):
                return ArbitrationEnvelope(
                    arbitration_id=envelope.arbitration_id,
                    level1=envelope.level1,
                    level2_required=True,
                    level2=Level2Decision(
                        "quick_tool", "home_scene_action", "consistent", 1.0,
                    ),
                )
            if (
                "home_scene_action" in selectable
                and _ROUTINE_SCENE_PATTERN.search(_normalized_exact_text(text))
            ):
                return ArbitrationEnvelope(
                    arbitration_id=envelope.arbitration_id,
                    level1=envelope.level1,
                    level2_required=True,
                    level2=Level2Decision(
                        "quick_tool", "home_scene_action", "consistent", 1.0,
                    ),
                )
            result = await self.classifier.classify(text, envelope)
            if not isinstance(result, dict) or set(result) != {
                "decision", "quick_tool_id", "confidence",
            }:
                return _escalated_level2(envelope)
            decision = str(result["decision"])
            tool_id = str(result["quick_tool_id"])
            confidence_value = result["confidence"]
            if (
                decision not in {"quick_tool", "openclaw"}
                or isinstance(confidence_value, bool)
                or not isinstance(confidence_value, (int, float))
            ):
                return _escalated_level2(envelope)
            confidence = float(confidence_value)
            if not 0 <= confidence <= 1:
                return _escalated_level2(envelope)
            if decision == "openclaw":
                if tool_id != "none":
                    return _escalated_level2(envelope)
                return _escalated_level2(envelope, confidence)

            selectable = {
                bundle["id"]: bundle for bundle in self.registry.list_selectable("4b")
            }
            bundle = selectable.get(tool_id)
            if bundle is None:
                return _escalated_level2(envelope, confidence)
            # An explicit L1 action must not be downgraded into a read-only tool.
            # Other L1 handoff values are transition-phrase hints only: L2 may
            # safely refine them to a registry-constrained device action.
            if envelope.level1.handoff == "action" and bundle["permissions"]["risk"] != "device_action":
                return _escalated_level2(envelope, confidence)
            required_confidence = max(
                self.min_confidence,
                float(bundle["selector"]["min_confidence"]["4b"]),
            )
            if confidence < required_confidence:
                return _escalated_level2(envelope, confidence)
            relation = (
                "consistent"
                if envelope.level1.handoff in {"lookup", "analyze"}
                else "overridden"
            )
            return ArbitrationEnvelope(
                arbitration_id=envelope.arbitration_id,
                level1=envelope.level1,
                level2_required=True,
                level2=Level2Decision(
                    decision="quick_tool", quick_tool_id=tool_id,
                    relation=relation, confidence=confidence,
                ),
            )
        except Exception:
            return _escalated_level2(envelope)
