"""Resolve one authoritative execution owner for a routed request."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arbitration import ArbitrationEnvelope
from capability_registry import CapabilityRegistry, CapabilityValidationError
from router import Route, RouteDecision


@dataclass(frozen=True)
class ExecutionPlan:
    capability: str
    capability_version: int
    executor: str
    producer: str
    phase: str
    transition: str
    progress: str
    long_wait: str
    bundle: dict[str, Any] | None = None
    escalation_reason: str = ""
    arbitration_id: str = ""
    decision_tier: str = "legacy"
    execution_class: str = "legacy"
    tool_class: str = "none"
    selector_tier: str = "openclaw_only"
    risk_class: str = "unrecorded"


def resolve_fallback_plan(
    plan: ExecutionPlan, *, reason: str, fallback_allowed: bool,
) -> ExecutionPlan:
    fallback = ((plan.bundle or {}).get("execution") or {}).get("fallback")
    if not fallback_allowed or fallback != "openclaw":
        raise CapabilityValidationError("capability fallback is not allowed")
    # The current OpenClaw jarvis agent has unrestricted exec. Until a
    # capability-bound execution profile can enforce allowed/forbidden tools,
    # a failed Quick Tool must not cross that permission boundary.
    raise CapabilityValidationError("restricted fallback profile is unavailable")


def _quick_tool_plan(
    envelope: ArbitrationEnvelope,
    registry: CapabilityRegistry,
    *,
    capability_id: str,
    decision_tier: str,
) -> ExecutionPlan:
    selectable = {
        bundle["id"]: bundle
        for bundle in registry.list_selectable(decision_tier)
    }
    bundle = selectable.get(capability_id)
    if bundle is None:
        raise CapabilityValidationError("quick tool is not selectable by decision tier")
    execution = bundle["execution"]
    delivery = bundle["delivery"]
    return ExecutionPlan(
        capability=bundle["id"],
        capability_version=bundle["version"],
        executor=execution["primary"],
        producer=execution["producer"],
        phase="execution",
        transition=delivery["transition"],
        progress=delivery["progress"],
        long_wait="Quick Tool还在读取数据，你再等我一下。",
        bundle=bundle,
        arbitration_id=envelope.arbitration_id,
        decision_tier=decision_tier,
        execution_class="quick_tool",
        tool_class=bundle["taxonomy"]["execution_kind"],
        selector_tier=bundle["selector"]["tier"],
        risk_class=bundle["permissions"]["risk"],
    )


def resolve_arbitration_plan(
    envelope: ArbitrationEnvelope,
    registry: CapabilityRegistry,
) -> ExecutionPlan:
    level1 = envelope.level1
    if level1.decision == "chat":
        if envelope.level2_required or envelope.level2 is not None:
            raise CapabilityValidationError("chat envelope cannot have level2")
        return ExecutionPlan(
            "none", 0, "local_4b", "local_4b", "generation",
            "", "本地模型正在整理回答。", "回答还在生成，你再等我一下。",
            arbitration_id=envelope.arbitration_id,
            decision_tier="0.8b", execution_class="local_chat",
        )
    if level1.decision == "quick_tool":
        if envelope.level2_required or envelope.level2 is not None:
            raise CapabilityValidationError("level1 quick tool cannot have level2")
        return _quick_tool_plan(
            envelope, registry,
            capability_id=level1.quick_tool_id, decision_tier="0.8b",
        )
    if level1.decision != "handoff" or not envelope.level2_required or envelope.level2 is None:
        raise CapabilityValidationError("handoff envelope requires level2")
    level2 = envelope.level2
    if level2.decision == "quick_tool":
        return _quick_tool_plan(
            envelope, registry,
            capability_id=level2.quick_tool_id, decision_tier="4b",
        )
    if level2.decision != "openclaw" or level2.quick_tool_id != "none":
        raise CapabilityValidationError("invalid level2 execution owner")
    return ExecutionPlan(
        "none", 0, "openclaw", "openclaw", "planning",
        "", "OpenClaw正在处理并核验结果。", "任务还在处理，你再等我一下。",
        arbitration_id=envelope.arbitration_id,
        decision_tier="openclaw", execution_class="openclaw",
        escalation_reason=level2.relation,
    )


def resolve_execution_plan(decision: RouteDecision, registry: CapabilityRegistry) -> ExecutionPlan:
    if decision.capability != "none":
        bundle = registry.active(decision.capability)
        if bundle is None:
            raise CapabilityValidationError("unknown active capability")
        routing = bundle["routing"]
        if routing["route"] != decision.route.value or routing["operation"] != decision.intent:
            raise CapabilityValidationError("capability route and operation mismatch")
        execution = bundle["execution"]
        delivery = bundle["delivery"]
        return ExecutionPlan(
            capability=bundle["id"], capability_version=bundle["version"],
            executor=execution["primary"], producer=execution["producer"], phase="execution",
            transition=delivery["transition"], progress=delivery["progress"],
            long_wait="设备目录还在读取，你再等我一下。", bundle=bundle,
        )

    if decision.route is Route.CAMERA:
        return ExecutionPlan("none", 0, "miloco", "miloco", "execution", "好的主人，我看一下。", "Miloco正在读取并分析画面记录。", "画面还在分析，你再等我一下。")
    if decision.route is Route.LOCAL_CHAT:
        return ExecutionPlan("none", 0, "local_4b", "local_4b", "generation", "好的主人，我想一下。", "本地 4B 正在整理回答。", "回答还在生成，你再等我一下。")
    if decision.route in {Route.HOME, Route.TASK, Route.WEB_QUERY}:
        transition = "好的主人，我来处理。" if decision.intent in {"action", "create", "update", "delete"} else "好的主人，我查一下。"
        return ExecutionPlan("none", 0, "openclaw", "openclaw", "planning", transition, "OpenClaw正在处理并核验结果。", "任务还在处理，你再等我一下。")
    return ExecutionPlan("none", 0, "native", "gateway", "execution", "", "", "")
