"""OpenAI-compatible HTTP surface for the Jarvis bridge — with OpenClaw WS routing."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
import urllib.request
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import websockets
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from capability_defaults import install_defaults
from capability_registry import CapabilityRegistry, CapabilityValidationError
from capability_validation import CapabilityValidator
from arbitration import (
    ArbitrationEnvelope, Level1Arbitrator, Level1Decision, Level2Arbitrator,
    OllamaLevel1Classifier, OllamaLevel2Classifier,
    build_level1_prompt, build_level2_prompt,
)
from execution_plan import (
    ExecutionPlan, resolve_arbitration_plan,
    resolve_execution_plan, resolve_fallback_plan,
)
from feedback_loop import (
    FeedbackLedger,
)
from home_device_control import control_device
from home_scene_control import trigger_ac_sleep_scene
from evolution_review_v2 import (
    EvolutionReview, append_evolution_review,
    build_evolution_review_prompt, parse_evolution_review,
)
from jarvis_bridge import default_run_home, extract_user_text, openai_response
from local_chat import LocalChatBackend
from recipe_adapters import (
    camera_inventory_zh, hamster_recent_activity_zh, external_home_device_list,
    external_home_hamster_recent, external_home_turtle_recent, turtle_recent_activity_zh,
)
from recipe_runtime import RecipeExecutionError, RecipeRuntime
from review_job import openclaw_reviewer
from router import Route, RouteDecision, Router
from semantic_router import (
    OllamaIntentClassifier, SemanticRouter, _OUTPUT_SCHEMA, build_router_prompt,
)

async def _warm_ollama_model(url: str, model: str) -> None:
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(url, json={
            "model": model, "prompt": "", "stream": False,
            "keep_alive": -1, "think": False,
            "options": {"num_predict": 1, "num_ctx": 2048},
        })
        response.raise_for_status()


async def warm_arbitration_models() -> None:
    await asyncio.gather(
        _warm_ollama_model(
            os.getenv("JARVIS_ROUTER_URL", "http://127.0.0.1:11435/api/generate"),
            os.getenv("JARVIS_ROUTER_MODEL", "qwen35-router:0.8b"),
        ),
        _warm_ollama_model(
            os.getenv("JARVIS_LEVEL2_URL", "http://127.0.0.1:11434/api/generate"),
            os.getenv("JARVIS_LEVEL2_MODEL", "qwen35-4b-16k:latest"),
        ),
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await warm_arbitration_models()
    yield


app = FastAPI(
    title="MiGPT Hermes Jarvis Bridge",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


capability_registry = CapabilityRegistry(
    Path(os.getenv("JARVIS_CAPABILITY_STATE", "./state/capabilities"))
)
install_defaults(capability_registry)
intent_router = SemanticRouter(
    Router(),
    OllamaIntentClassifier(capability_registry=capability_registry),
    enabled=os.getenv("JARVIS_SEMANTIC_ROUTER", "0").lower() in {"1", "true", "yes"},
    min_confidence=float(os.getenv("JARVIS_ROUTER_MIN_CONFIDENCE", "0.8")),
)
level1_arbitrator = Level1Arbitrator(
    OllamaLevel1Classifier(capability_registry), capability_registry,
    min_confidence=float(os.getenv("JARVIS_LEVEL1_MIN_CONFIDENCE", "0.9")),
)
level2_arbitrator = Level2Arbitrator(
    OllamaLevel2Classifier(capability_registry), capability_registry,
    min_confidence=float(os.getenv("JARVIS_LEVEL2_MIN_CONFIDENCE", "0.9")),
)
local_chat = LocalChatBackend()
feedback_ledger = FeedbackLedger(
    Path(os.getenv("JARVIS_FEEDBACK_STATE", "./state/route-feedback"))
)
recipe_runtime = RecipeRuntime(
    tools={
        "external_home.device_list": external_home_device_list,
        "external_home.turtle_recent": external_home_turtle_recent,
        "external_home.hamster_recent": external_home_hamster_recent,
    },
    templates={
        "camera_inventory_zh": camera_inventory_zh,
        "turtle_recent_activity_zh": turtle_recent_activity_zh,
        "hamster_recent_activity_zh": hamster_recent_activity_zh,
    },
)


def capability_replay_runner(bundle: dict[str, Any]) -> dict[str, Any]:
    routing = bundle["routing"]
    examples = list(routing["examples"])
    candidate_pairs = "\n".join(
        f'“{text}”=>{routing["route"]}/{routing["operation"]}/{bundle["id"]}'
        for text in examples
    )
    system = build_router_prompt(capability_registry=capability_registry)
    system += "\n待验证候选能力示例：\n" + candidate_pairs
    correct = 0
    valid = 0
    for text in examples:
        payload = json.dumps({
            "model": os.getenv("JARVIS_ROUTER_MODEL", "qwen35-router:0.8b"),
            "system": system, "prompt": text, "stream": False,
            "format": _OUTPUT_SCHEMA, "think": False, "keep_alive": "30m",
            "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 40},
        }, ensure_ascii=False).encode()
        request = urllib.request.Request(
            os.getenv("JARVIS_ROUTER_URL", "http://127.0.0.1:11435/api/generate"),
            data=payload, headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                outer = json.load(response)
            parsed = json.loads(str(outer.get("response") or ""))
            schema_valid = (
                isinstance(parsed, dict)
                and set(parsed) == {"intent", "operation", "capability", "confidence"}
                and isinstance(parsed.get("confidence"), (int, float))
                and not isinstance(parsed.get("confidence"), bool)
                and 0 <= parsed["confidence"] <= 1
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            schema_valid = False
            parsed = {}
        valid += int(schema_valid)
        correct += int(schema_valid and (
            parsed.get("intent"), parsed.get("operation"), parsed.get("capability")
        ) == (routing["route"], routing["operation"], bundle["id"]))
    count = len(examples)
    accuracy = correct / count if count else 0.0
    valid_rate = valid / count if count else 0.0
    return {
        "passed": count > 0 and accuracy == 1.0 and valid_rate == 1.0,
        "sample_count": count,
        "accuracy": accuracy,
        "valid_json_rate": valid_rate,
    }


def _capability_validator() -> CapabilityValidator:
    return CapabilityValidator(
        capability_registry.root / "validation-reports",
        capability_registry,
        recipe_runtime,
        replay_runner=capability_replay_runner,
    )


_lock = asyncio.Lock()

OPENCLAW_WS = os.getenv("OPENCLAW_WS", "ws://127.0.0.1:18789/stream")
USE_OPENCLAW = os.getenv("USE_OPENCLAW", "1").lower() in {"1", "true", "yes"}
PROGRESS_AFTER_SECONDS = float(os.getenv("JARVIS_PROGRESS_AFTER", "5"))
LONG_WAIT_AFTER_SECONDS = float(os.getenv("JARVIS_LONG_WAIT_AFTER", "15"))
ACK_DELAY_SECONDS = float(os.getenv("JARVIS_ACK_DELAY", "0"))
TWO_STAGE_ARBITRATION = os.getenv(
    "JARVIS_TWO_STAGE_ARBITRATION", "0",
).lower() in {"1", "true", "yes"}


def _load_api_key() -> str:
    direct = os.getenv("JARVIS_API_KEY", "").strip()
    if direct:
        return direct
    key_file = os.getenv("JARVIS_API_KEY_FILE", "").strip()
    if key_file:
        try:
            return Path(key_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


_api_key = _load_api_key()

_EVOLUTION_DASHBOARD = Path(__file__).with_name("evolution_dashboard.html")
_EVOLUTION_STATE = Path(os.getenv("JARVIS_FEEDBACK_STATE", "./state/route-feedback"))
_EVOLUTION_AUDIT = _EVOLUTION_STATE / "manual-tier-audit.jsonl"
_OPENCLAW_PROMPT_ROOT = Path(os.getenv(
    "JARVIS_OPENCLAW_WORKSPACE", str(Path.home() / ".openclaw/workspace-jarvis"),
))
_TIER_ORDER = ("openclaw_only", "4b_eligible", "08b_eligible")


def _read_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _prompt_version(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _latest_prompt_projection_time() -> str:
    paths = [Path(__file__).with_name("arbitration.py")]
    paths.extend(capability_registry.active_root.glob("*.json"))
    updated = 0.0
    for path in paths:
        try:
            updated = max(updated, path.stat().st_mtime)
        except OSError:
            continue
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(updated)) if updated else ""


def _openclaw_prompt_projection() -> tuple[str, list[str], str]:
    sources: list[str] = []
    sections: list[str] = []
    updated = 0.0
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md"):
        path = _OPENCLAW_PROMPT_ROOT / name
        try:
            content = path.read_text(encoding="utf-8").strip()
            stat = path.stat()
        except OSError:
            continue
        if content:
            sources.append(name)
            sections.append(f"## {name}\n\n{content}")
            updated = max(updated, stat.st_mtime)
    text = "\n\n".join(sections) or "（OpenClaw Workspace提示词不可用）"
    updated_at = (
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(updated))
        if updated else ""
    )
    return text, sources, updated_at


def _append_evolution_audit(event: dict[str, Any]) -> None:
    _EVOLUTION_AUDIT.parent.mkdir(parents=True, exist_ok=True)
    _EVOLUTION_AUDIT.parent.chmod(0o700)
    descriptor = os.open(_EVOLUTION_AUDIT, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        _EVOLUTION_AUDIT.chmod(0o600)


def _discovery_evidence_by_key() -> dict[str, dict[str, int]]:
    discoveries = _read_json_file(_EVOLUTION_STATE / "quick-tool-discoveries.json", [])
    result: dict[str, dict[str, int]] = {}
    for item in discoveries if isinstance(discoveries, list) else []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        result[key] = {
            "evidence_count": len(item.get("evidence_ids") or []),
            "days_count": len(item.get("days") or []),
            "expressions_count": len(item.get("examples") or []),
        }
    return result


def _dashboard_tool(
    bundle: dict[str, Any], proposal: dict[str, Any] | None,
    evidence: dict[str, dict[str, int]],
) -> dict[str, Any]:
    selector = bundle.get("selector") if isinstance(bundle.get("selector"), dict) else {}
    taxonomy = bundle.get("taxonomy") if isinstance(bundle.get("taxonomy"), dict) else {}
    permissions = bundle.get("permissions") if isinstance(bundle.get("permissions"), dict) else {}
    execution_raw = bundle.get("execution")
    execution: dict[str, Any] = execution_raw if isinstance(execution_raw, dict) else {}
    tier = str(selector.get("tier") or "openclaw_only")
    capability_id = str(bundle.get("id") or "")
    candidate_bundle = proposal.get("capability") if isinstance(proposal, dict) else None
    candidate_tier = ""
    candidate_version = 0
    if isinstance(candidate_bundle, dict):
        candidate_selector = candidate_bundle.get("selector")
        candidate_tier = str(
            candidate_selector.get("tier") if isinstance(candidate_selector, dict) else ""
        )
        candidate_version = int(candidate_bundle.get("version") or 0)
    metrics = evidence.get(capability_id, {})
    next_tiers: list[str] = []
    if tier in _TIER_ORDER:
        index = _TIER_ORDER.index(tier)
        if index > 0:
            next_tiers.append(_TIER_ORDER[index - 1])
        if index + 1 < len(_TIER_ORDER):
            next_tiers.append(_TIER_ORDER[index + 1])
    threshold = {
        "openclaw_only": {"turns": 5, "days": 2, "expressions": 3},
        "4b_eligible": {"turns": 10, "days": 3, "expressions": 5},
        "08b_eligible": None,
    }.get(tier)
    history = capability_registry.history(capability_id)
    report_root = capability_registry.root / "validation-reports"
    latest_report: dict[str, Any] | None = None
    latest_report_time = ""
    if report_root.exists():
        for report_path in sorted(
            report_root.glob("*.json"), key=lambda path: path.stat().st_mtime,
            reverse=True,
        ):
            report = _read_json_file(report_path, {})
            if report.get("capability_id") != capability_id:
                continue
            latest_report = report
            latest_report_time = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(report_path.stat().st_mtime),
            )
            break
    gate_aliases = {
        "replay": "Replay回放",
        "dangerous_negatives": "危险负例",
        "shadow": "Shadow对照",
        "security": "Security安全检查",
    }
    report_gates = latest_report.get("gates") if isinstance(latest_report, dict) else {}
    validation_gates = []
    for gate_key, label in gate_aliases.items():
        matched = next((
            value for name, value in report_gates.items()
            if name == gate_key or name.startswith(gate_key + "_")
        ), None) if isinstance(report_gates, dict) else None
        validation_gates.append({
            "key": gate_key, "label": label,
            "status": (
                "passed" if isinstance(matched, dict) and matched.get("passed") is True
                else "failed" if isinstance(matched, dict)
                else "not_run"
            ),
        })
    validation_status = (
        "passed" if isinstance(latest_report, dict) and latest_report.get("passed") is True
        else "failed" if isinstance(latest_report, dict)
        else "not_run"
    )
    lifecycle_stage = (
        "validation" if isinstance(proposal, dict)
        else "active" if history
        else "evidence"
    )
    return {
        "id": capability_id,
        "version": int(bundle.get("version") or 0),
        "status": str(bundle.get("status") or "active"),
        "active": True,
        "candidate": isinstance(proposal, dict),
        "selector_tier": tier,
        "description": str(selector.get("description") or capability_id),
        "risk": str(permissions.get("risk") or ""),
        "execution_kind": str(taxonomy.get("execution_kind") or ""),
        "executor": str(execution.get("primary") or ""),
        "producer": str(execution.get("producer") or ""),
        "domain_tags": [str(tag) for tag in taxonomy.get("domain_tags") or []],
        "evidence_count": int(metrics.get("evidence_count") or 0),
        "days_count": int(metrics.get("days_count") or 0),
        "expressions_count": int(metrics.get("expressions_count") or 0),
        "threshold": threshold,
        "next_tiers": next_tiers if proposal is None else [],
        "candidate_version": candidate_version,
        "candidate_target_tier": candidate_tier,
        "candidate_evidence_count": len(proposal.get("evidence_ids") or []) if isinstance(proposal, dict) else 0,
        "lifecycle": {
            "stage": lifecycle_stage,
            "active_version": int(bundle.get("version") or 0),
            "candidate_version": candidate_version,
            "candidate_target_tier": candidate_tier,
            "previous_versions": [int(item.get("version") or 0) for item in history],
            "evidence": {
                "turns": int(metrics.get("evidence_count") or 0),
                "days": int(metrics.get("days_count") or 0),
                "expressions": int(metrics.get("expressions_count") or 0),
            },
            "validation": {
                "status": validation_status,
                "checked_at": latest_report_time,
                "report": str(latest_report.get("report_hash") or "")[:12] if isinstance(latest_report, dict) else "",
                "gates": validation_gates,
            },
        },
    }


def _evolution_dashboard_payload() -> dict[str, Any]:
    active = capability_registry.list_active()
    proposals = {
        str(item.get("capability", {}).get("id") or ""): item
        for item in capability_registry.list_candidates()
        if isinstance(item, dict) and isinstance(item.get("capability"), dict)
    }
    evidence = _discovery_evidence_by_key()
    tools = [_dashboard_tool(bundle, proposals.get(str(bundle.get("id") or "")), evidence) for bundle in active]
    grouped = {tier: [] for tier in _TIER_ORDER}
    for tool in tools:
        grouped.get(tool["selector_tier"], grouped["openclaw_only"]).append(tool)

    l1_prompt = build_level1_prompt(capability_registry)
    sample_envelope = ArbitrationEnvelope(
        arbitration_id="dashboard-preview",
        level1=Level1Decision("handoff", "none", "general", 1.0),
        level2_required=True,
    )
    l2_prompt = build_level2_prompt(capability_registry, sample_envelope)
    openclaw_prompt, openclaw_sources, openclaw_updated = _openclaw_prompt_projection()
    last_run = _read_json_file(_EVOLUTION_STATE / "last-evolution-v2.json", {})
    generated_at = _utc_timestamp()
    prompt_updated = _latest_prompt_projection_time()
    columns = [
        {
            "id": "08b_eligible", "label": "0.8B",
            "role": "一级仲裁与认证直选",
            "model": os.getenv("JARVIS_ROUTER_MODEL", "qwen35-router:0.8b"),
            "status": "active", "prompt": {
                "text": l1_prompt, "version": _prompt_version(l1_prompt),
                "updated_at": prompt_updated, "sources": ["arbitration.py", "Capability Registry"],
            }, "tools": grouped["08b_eligible"],
        },
        {
            "id": "4b_eligible", "label": "4B",
            "role": "二级仲裁与受约束执行",
            "model": os.getenv("JARVIS_LEVEL2_MODEL", "qwen35-4b-16k:latest"),
            "status": "active", "prompt": {
                "text": l2_prompt, "version": _prompt_version(l2_prompt),
                "updated_at": prompt_updated, "sources": ["arbitration.py", "Capability Registry"],
            }, "tools": grouped["4b_eligible"],
        },
        {
            "id": "openclaw_only", "label": "OPENCLAW",
            "role": "贾维斯本人 / 开放任务执行",
            "model": "OpenClaw Jarvis Agent",
            "status": "active", "prompt": {
                "text": openclaw_prompt, "version": _prompt_version(openclaw_prompt),
                "updated_at": openclaw_updated, "sources": openclaw_sources,
            }, "tools": grouped["openclaw_only"],
        },
    ]
    return {
        "generated_at": generated_at,
        "architecture": {
            "title": "当前请求处理链",
            "flow": ["08b_eligible", "4b_eligible", "openclaw_only"],
            "summary": "0.8B只做一级仲裁；4B选择认证Quick Tool；其余交给OpenClaw本人完成。",
        },
        "last_run": last_run,
        "columns": columns,
    }


def _stage_event(
    base: dict[str, Any], *, phase: str, producer: str, status: str,
    data: dict[str, Any] | None = None,
) -> str:
    payload = {
        **base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "jarvis": {
            "event": "stage", "phase": phase, "producer": producer,
            "status": status, "visibility": "ui", "speak": False,
            "data": data or {},
        },
    }
    return "data: " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def _sse_event(base: dict[str, Any], delta: dict[str, Any], finish_reason: str | None = None, index: int = 0) -> str:
    chunk = dict(base)
    chunk["choices"] = [{"index": index, "delta": delta, "finish_reason": finish_reason}]
    return f"data: {json.dumps(chunk, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _clean_voice_agent_result(text: str) -> str:
    """Remove model-facing planning prose and Markdown from spoken results."""
    text = str(text or "").strip()
    markers = (
        "我需要给用户一个简洁的回复。",
        "我需要给用户一个简短的回复。",
        "我需要用简短自然中文回答用户。",
        "需要给用户简洁的回复。",
        "需要给用户一个简洁的回复。",
        "需要给用户简短的回复。",
        "需要给用户一个简短的回复。",
        "我应该简洁地告诉用户结果。",
        "我应该给用户简洁的回复。",
        "现在给用户回复。",
        "现在我可以给用户一个简洁的回答了。",
        "用户问的是数量和清单，我需要按真实情况回答。",
        "最终回复：",
    )
    for marker in markers:
        if marker in text:
            text = text.rsplit(marker, 1)[-1].strip()
    # Some providers prepend an English/internal summary before a clean Chinese
    # answer. When that happens, prefer the final short Chinese sentence.
    if re.search(r"[A-Za-z]{4,}.*[\u4e00-\u9fff]", text, flags=re.S):
        chinese_sentences = re.findall(r"[^。！？!?.]*[\u4e00-\u9fff][^。！？!?.]*[。！？!?]", text)
        if chinese_sentences:
            text = chinese_sentences[-1].strip()
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I)
    text = re.sub(r"```[\s\S]*?```", "", text)
    text = re.sub(r"[（(]\s*(?:on|value)\s*=\s*(?:true|false)\s*[）)]", "", text, flags=re.I)
    text = re.sub(r"(?:查询完毕|查询完成)[，,。 ]*不执行任何操作[。.]?", "", text)
    text = re.split(r"\s*(?:⚠️|🛠️|tool\s+(?:call|error)|run\s+(?:node|python)\s+script)\s*", text, maxsplit=1, flags=re.I)[0]
    text = re.sub(r"[*_`~#]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


def _progress_phrases(decision: RouteDecision) -> tuple[str, str, str]:
    """Truthful spoken progress selected from the model's structured decision."""
    if decision.route is Route.TASK and decision.intent in {"list", "query"}:
        return (
            "好的主人，我查一下当前任务。",
            "我正在读取任务列表。",
            "任务列表还在读取，你再等我一下。",
        )
    if decision.route is Route.TASK:
        return (
            "好的主人，我来处理这个任务。",
            "OpenClaw正在规划并核验任务设置。",
            "任务还在处理，你再等我一下。",
        )
    if decision.route is Route.CAMERA:
        return (
            "好的主人，我看一下。",
            "External home backend正在读取并分析画面记录。",
            "画面还在分析，你再等我一下。",
        )
    if decision.route is Route.HOME and decision.intent == "action":
        return (
            "好的主人，我来处理。",
            "OpenClaw正在调用External home backend执行并核验设备状态。",
            "设备操作还在核验，你再等我一下。",
        )
    if decision.route is Route.WEB_QUERY:
        return (
            "好的主人，我查一下。",
            "OpenClaw正在查询并整理信息。",
            "信息还在查询，你再等我一下。",
        )
    if decision.route is Route.LOCAL_CHAT:
        return (
            "好的主人，我想一下。",
            "本地 9B 正在整理回答。",
            "回答还在生成，你再等我一下。",
        )
    return (
        "好的主人，我查一下。",
        "OpenClaw正在读取并核验设备状态。",
        "设备状态还在核验，你再等我一下。",
    )


async def _with_progress(
    source,
    base: dict[str, Any],
    phrases: tuple[str, str, str],
    *,
    ack_delay: float = ACK_DELAY_SECONDS,
    progress_after: float = PROGRESS_AFTER_SECONDS,
    long_wait_after: float = LONG_WAIT_AFTER_SECONDS,
):
    """Add bounded, truthful voice progress around a slow async stream.

    The wrapped source must expose only a grounded final result. Internal model
    deltas stay buffered inside the OpenClaw adapter.
    """
    ack, progress, long_wait = phrases
    iterator = source.__aiter__()
    pending = asyncio.create_task(iterator.__anext__())
    try:
        if ack_delay <= 0:
            # Once the 0.8B arbiter has selected a slow executor, acknowledge
            # immediately with the route-specific phrase before waiting on it.
            yield _sse_event(base, {"role": "assistant", "content": ack})
            yield _sse_event(base, {})
        else:
            try:
                # Optionally allow truly fast deterministic queries a small
                # head start so they can answer without a redundant ack.
                first = await asyncio.wait_for(asyncio.shield(pending), timeout=ack_delay)
                yield _sse_event(base, {"role": "assistant"})
                yield first
                async for chunk in iterator:
                    yield chunk
                return
            except asyncio.TimeoutError:
                yield _sse_event(base, {"role": "assistant", "content": ack})
                # Some SDKs release one content event only after the next event.
                yield _sse_event(base, {})
        try:
            first = await asyncio.wait_for(
                asyncio.shield(pending), timeout=max(0.0, progress_after - ack_delay)
            )
        except asyncio.TimeoutError:
            yield _sse_event(base, {"content": progress})
            yield _sse_event(base, {})
            try:
                first = await asyncio.wait_for(
                    asyncio.shield(pending),
                    timeout=max(0.0, long_wait_after - progress_after),
                )
            except asyncio.TimeoutError:
                yield _sse_event(base, {"content": long_wait})
                yield _sse_event(base, {})
                first = await pending
        yield first
        async for chunk in iterator:
            yield chunk
    except StopAsyncIteration:
        return
    finally:
        if not pending.done():
            pending.cancel()


async def _visual_result_stream(body: dict[str, Any], base: dict[str, Any]):
    async with _lock:
        content = await asyncio.to_thread(run_external_home_query, extract_user_text(body))
    if content:
        yield _sse_event(base, {"content": content})
    yield _sse_event(base, {}, "stop")
    yield "data: [DONE]\n\n"


def _arbitration_ledger_fields(
    arbitration: ArbitrationEnvelope | None,
    plan: ExecutionPlan | None,
    timing: dict[str, int] | None = None,
) -> dict[str, Any]:
    if arbitration is None or plan is None:
        return {}
    timing = timing or {}
    level1 = {
        "decision": arbitration.level1.decision,
        "quick_tool_id": arbitration.level1.quick_tool_id,
        "handoff": arbitration.level1.handoff,
        "confidence": arbitration.level1.confidence,
    }
    if "level1_elapsed_ms" in timing:
        level1["elapsed_ms"] = timing["level1_elapsed_ms"]
    level2 = None
    if arbitration.level2 is not None:
        level2 = {
            "decision": arbitration.level2.decision,
            "quick_tool_id": arbitration.level2.quick_tool_id,
            "relation": arbitration.level2.relation,
            "confidence": arbitration.level2.confidence,
        }
        if "level2_elapsed_ms" in timing:
            level2["elapsed_ms"] = timing["level2_elapsed_ms"]
    return {
        "arbitration_id": arbitration.arbitration_id,
        "level1": level1,
        "level2": level2,
        "decision_tier": plan.decision_tier,
        "execution_class": plan.execution_class,
        "tool_class": plan.tool_class,
        "selector_tier": plan.selector_tier,
        "risk_class": plan.risk_class,
    }


async def _arbitrate_two_stage(
    text: str, timing: dict[str, int] | None = None,
) -> ArbitrationEnvelope:
    timing = timing if timing is not None else {}
    l1_started = time.perf_counter()
    envelope = await level1_arbitrator.decide(text)
    timing["level1_elapsed_ms"] = round((time.perf_counter() - l1_started) * 1000)
    if envelope.level2_required:
        l2_started = time.perf_counter()
        envelope = await level2_arbitrator.decide(text, envelope)
        timing["level2_elapsed_ms"] = round((time.perf_counter() - l2_started) * 1000)
    return envelope


def _prepare_two_stage_body(
    body: dict[str, Any], user_text: str,
) -> tuple[str, dict[str, Any]]:
    session_id = feedback_ledger.resolve_session(body.get("session_id"))
    history_messages: list[dict[str, str]] = []
    for turn in feedback_ledger.session_turns(session_id)[-10:]:
        history_messages.extend([
            {"role": "user", "content": str(turn["request"])},
            {"role": "assistant", "content": str(turn["answer"])},
        ])
    execution_body = dict(body)
    execution_body["session_id"] = session_id
    execution_body["messages"] = history_messages + [
        {"role": "user", "content": user_text},
    ]
    return session_id, execution_body


async def _recording_stream(
    source, *, request: str, decision: RouteDecision,
    session_id: str | None = None, success_override: bool | None = None,
    execution_plan: ExecutionPlan | None = None,
    execution_plan_state: dict[str, ExecutionPlan] | None = None,
    arbitration: ArbitrationEnvelope | None = None,
    audit_route: str | None = None,
    audit_intent: str | None = None,
    arbitration_timing: dict[str, int] | None = None,
):
    """Pass SSE through and expose a non-spoken stable turn id before DONE."""
    answer = ""
    tool_trace: list[dict[str, Any]] = []
    tool_started: dict[tuple[str, str], list[float]] = {}
    execution_started = time.perf_counter()
    success = False
    saved = False
    try:
        async for event in source:
            if isinstance(event, str) and event.startswith("data: "):
                payload = event[6:].strip()
                if payload == "[DONE]":
                    success = True
                    for (provider, tool), starts in tool_started.items():
                        for _started in starts:
                            tool_trace.append({
                                "provider": provider, "tool": tool,
                                "status": "started",
                            })
                    tool_started.clear()
                    final_success = True if success_override is None else success_override
                    effective_plan = (
                        (execution_plan_state or {}).get("plan") or execution_plan
                    )
                    default_phase, default_producer = {
                        Route.CAMERA: ("execution", "external_home"),
                        Route.HOME: ("planning", "openclaw"),
                        Route.WEB_QUERY: ("planning", "openclaw"),
                        Route.TASK: ("planning", "openclaw"),
                        Route.LOCAL_CHAT: ("generation", "local_4b"),
                    }.get(decision.route, ("execution", "gateway"))
                    phase = effective_plan.phase if effective_plan else default_phase
                    producer = effective_plan.producer if effective_plan else default_producer
                    stage_data = ({
                        "capability": effective_plan.capability,
                        "capability_version": effective_plan.capability_version,
                        "planned_executor": (
                            effective_plan.execution_class
                            if effective_plan.execution_class != "legacy"
                            else effective_plan.executor
                        ),
                        "execution_class": effective_plan.execution_class,
                        "tool_class": effective_plan.tool_class,
                        "execution_kind": effective_plan.tool_class,
                        "selector_tier": effective_plan.selector_tier,
                        "risk_class": effective_plan.risk_class,
                        "decision_tier": effective_plan.decision_tier,
                        "arbitration_id": effective_plan.arbitration_id,
                        "escalation_reason": effective_plan.escalation_reason,
                    } if effective_plan else {})
                    yield _stage_event(
                        {
                            "id": "chatcmpl-jarvis", "object": "chat.completion.chunk",
                            "created": 0, "model": "hermes-jarvis",
                        },
                        phase=phase, producer=producer,
                        status="completed" if final_success else "failed",
                        data=stage_data,
                    )
                    turn = feedback_ledger.save_turn({
                        "session_id": session_id,
                        "request": request,
                        "route": audit_route or decision.route.value,
                        "intent": audit_intent or decision.intent,
                        "executor": effective_plan.executor if effective_plan else _executor_for(decision),
                        "producer": effective_plan.producer if effective_plan else _executor_for(decision),
                        "capability": effective_plan.capability if effective_plan else decision.capability,
                        "capability_version": effective_plan.capability_version if effective_plan else 0,
                        "escalation_reason": effective_plan.escalation_reason if effective_plan else "",
                        "tool_trace": tool_trace,
                        "execution_elapsed_ms": round(
                            (time.perf_counter() - execution_started) * 1000
                        ),
                        **_arbitration_ledger_fields(
                            arbitration, effective_plan, arbitration_timing,
                        ),
                        "answer": answer,
                        "success": final_success,
                    })
                    saved = True
                    metadata = {
                        "id": "chatcmpl-jarvis",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "hermes-jarvis",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
                        "jarvis": {
                            "stage": "recorded", "turn_id": turn["turn_id"],
                            "session_id": turn["session_id"],
                        },
                    }
                    yield "data: " + json.dumps(metadata, ensure_ascii=False, separators=(",", ":")) + "\n\n"
                    yield event
                    continue
                try:
                    frame = json.loads(payload)
                    jarvis = frame.get("jarvis") or {}
                    if jarvis.get("event") == "stage" and jarvis.get("phase") == "tool":
                        data = jarvis.get("data") or {}
                        provider = str(jarvis.get("producer") or "")
                        tool = str(data.get("tool") or "")
                        status = str(jarvis.get("status") or "")
                        key = (provider, tool)
                        if status == "started":
                            tool_started.setdefault(key, []).append(time.perf_counter())
                        elif status in {"completed", "failed"}:
                            starts = tool_started.get(key) or []
                            started = starts.pop(0) if starts else None
                            item: dict[str, Any] = {
                                "provider": provider, "tool": tool, "status": status,
                            }
                            if started is not None:
                                item["elapsed_ms"] = round(
                                    (time.perf_counter() - started) * 1000
                                )
                            tool_trace.append(item)
                    delta = ((frame.get("choices") or [{}])[0].get("delta") or {})
                    answer += str(delta.get("content") or "")
                except (json.JSONDecodeError, AttributeError):
                    pass
            yield event
    finally:
        if not saved:
            effective_plan = (
                (execution_plan_state or {}).get("plan") or execution_plan
            )
            feedback_ledger.save_turn({
                "session_id": session_id,
                "request": request,
                "route": audit_route or decision.route.value,
                "intent": audit_intent or decision.intent,
                "executor": effective_plan.executor if effective_plan else _executor_for(decision),
                "producer": effective_plan.producer if effective_plan else _executor_for(decision),
                "capability": effective_plan.capability if effective_plan else decision.capability,
                "capability_version": effective_plan.capability_version if effective_plan else 0,
                "escalation_reason": effective_plan.escalation_reason if effective_plan else "",
                "tool_trace": tool_trace,
                "execution_elapsed_ms": round(
                    (time.perf_counter() - execution_started) * 1000
                ),
                **_arbitration_ledger_fields(
                    arbitration, effective_plan, arbitration_timing,
                ),
                "answer": answer,
                "success": success if success_override is None else success_override,
            })


def _result_to_sse(result: dict[str, Any], base: dict[str, Any]):
    content = str(((result.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    yield _sse_event(base, {"role": "assistant"})
    if content:
        yield _sse_event(base, {"content": content})
    yield _sse_event(base, {}, "stop")
    yield "data: [DONE]\n\n"


def _agent_extra_prompt(route: Route | None, surface: str = "speaker") -> str:
    interface = (
        "AIRI数字形象界面"
        if str(surface or "").strip().lower() == "airi"
        else "小爱音箱"
    )
    surface_identity = (
        "如果用户询问你在哪里或通过什么界面交流，必须原样回答“AIRI数字形象界面”，不要猜测具体终端设备。"
        if interface == "AIRI数字形象界面"
        else ""
    )
    common = (
        f"你正在通过{interface}与示例用户对话。{surface_identity}用简短自然中文回答，不要使用Markdown。"
        "调用工具时不要向用户叙述计划、步骤、命令或内部思考；"
        "工具全部完成后只给一到两句最终结果，并且最终一行必须严格以‘最终回复：’开头。"
    )
    if route is Route.TASK:
        return (
            common
            + "只处理明确的家庭任务创建、查询、修改、暂停、启用或删除。"
            "查询任务时先直接给出任务内容、状态和时间，不要只反问下一步；"
            "必须加载并严格遵循external_home-create-task或external_home-terminate-task；"
            "只有拿到真实task_id并完成相关rule或cron装配后才能声称创建成功。"
        )
    if route is Route.WEB_QUERY:
        return common + "只处理需要外部实时信息或联网检索的只读问题，不执行任何家庭设备、任务或其他副作用操作。"
    if route is None:
        return (
            common
            + "理解用户的开放式请求，只能使用当前Jarvis Agent已经授权的Skills和工具。"
            "只读任务必须基于真实工具结果；写入、控制、下单、支付或其他副作用必须遵循对应Skill的确认和审计要求。"
            "缺少所需工具或证据时如实说明，不得虚构查询、执行或成功结果。"
        )
    return (
        common
        + "只处理明确的家庭设备查询或控制。"
        "如果用户询问摄像头数量或清单，必须执行external_home-cli device list，"
        "只统计category严格等于camera的设备；不得把video-doorbell、speaker或其他带视频能力的设备算作摄像头，"
        "并按真实目录逐个列出名称和房间。"
        "如果用户询问宠物龟、乌龟、宠物仓鼠等宠物当前在做什么，必须先读取external_home-perception技能，"
        "默认执行external_home-cli perceive logs --since读取对应摄像头的近期感知记录，不得重新打开摄像头实时查看；"
        "仅当用户明确说现在看一下、打开摄像头看看时才实时感知。"
        "没有近期明确记录就如实说明，禁止猜测宠物数量、位置、动作或状态。"
    )


def _openclaw_tool_stage(event_payload: dict[str, Any]) -> dict[str, str] | None:
    if not isinstance(event_payload, dict) or event_payload.get("stream") != "item":
        return None
    data = event_payload.get("data") or {}
    tool = str(data.get("name") or "")[:64]
    phase = str(data.get("phase") or "")
    raw_status = str(data.get("status") or "").lower()
    if not tool or not all(ch.isalnum() or ch in "._-" for ch in tool):
        return None
    if phase == "start":
        status = "started"
    elif phase == "end":
        status = "failed" if raw_status in {"error", "failed", "cancelled"} else "completed"
    else:
        return None
    return {"tool": tool, "status": status}


async def _openclaw_ws_stream(
    body: dict[str, Any], model_id: str, route: Route | None = Route.HOME
):
    """Run one OpenClaw agent turn via the official Gateway RPC protocol."""

    try:
        ws = await asyncio.wait_for(
            websockets.connect(OPENCLAW_WS, ping_interval=None),
            timeout=5
        )
    except Exception as e:
        raise ConnectionError(f"OpenClaw WS连接失败: {e}") from e

    try:
        # Step 1: receive connect.challenge
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        challenge = json.loads(raw)
        if challenge.get("type") != "event" or challenge.get("event") != "connect.challenge":
            raise ValueError(f"Unexpected frame: {challenge}")

        # Official local backend identity. This is the Gateway's supported
        # device-auth bypass for a direct loopback backend connection.
        await ws.send(json.dumps({
            "type": "req",
            "id": "c1",
            "method": "connect",
            "params": {
                "client": {
                    "id": "gateway-client",
                    "mode": "backend",
                    "displayName": "jarvis-api",
                    "version": "2026.6.33",
                    "platform": "darwin",
                },
                "minProtocol": 4,
                "maxProtocol": 4,
                "role": "operator",
                "scopes": ["operator.admin"],
                "auth": {},
            }
        }))

        while True:
            result = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if result.get("type") == "res" and result.get("id") == "c1":
                if not result.get("ok"):
                    raise ValueError(f"OpenClaw连接失败: {result}")
                break

        # Official agent RPC. A stable session key lets speaker conversations
        # continue in the same OpenClaw session.
        messages = body.get("messages", [])
        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = m.get("content", "") or ""
                break

        run_id = str(uuid.uuid4())
        bridge_session_id = str(body.get("session_id") or "").strip()
        if not bridge_session_id or not all(ch.isalnum() or ch in "-_" for ch in bridge_session_id):
            raise ValueError("OpenClaw requires a valid Bridge session_id")
        # The isolated minimal-tool agent keeps one stable OpenClaw session per
        # Bridge conversation. Only the newest user text is sent because
        # OpenClaw itself owns the tool/planning history for this stable key.
        session_key = f"agent:jarvis:session-{bridge_session_id}"
        await ws.send(json.dumps({
            "type": "req", "id": "a1", "method": "agent",
            "params": {
                "message": user_text,
                "agentId": "jarvis",
                "sessionKey": session_key,
                "thinking": "off",
                "deliver": False,
                "timeout": 120,
                "cleanupBundleMcpOnRunEnd": True,
                "extraSystemPrompt": _agent_extra_prompt(route, body.get("surface", "speaker")),
                "idempotencyKey": run_id,
            }
        }))

        # Tool-capable cloud models may put plans and intermediate reasoning in
        # assistant deltas before calling tools. Buffer those events and speak
        # only the grounded final payload so TTS never exposes internal work.
        streamed_content = ""
        observed_tool_trace: list[dict[str, str]] = []
        while True:
            raw4 = await asyncio.wait_for(ws.recv(), timeout=150)
            frame = json.loads(raw4)
            if frame.get("type") == "event" and frame.get("event") == "agent":
                event_payload = frame.get("payload") or {}
                if event_payload.get("runId") != run_id:
                    continue
                tool_stage = _openclaw_tool_stage(event_payload)
                if tool_stage:
                    observed_tool_trace.append({
                        "provider": "openclaw",
                        "tool": tool_stage["tool"],
                        "status": tool_stage["status"],
                    })
                    yield _stage_event(
                        {
                            "id": "chatcmpl-jarvis", "object": "chat.completion.chunk",
                            "created": 0, "model": model_id,
                        },
                        phase="tool", producer="openclaw",
                        status=tool_stage["status"],
                        data={"tool": tool_stage["tool"]},
                    )
                    continue
                if event_payload.get("stream") == "assistant":
                    data = event_payload.get("data") or {}
                    delta = str(data.get("delta") or "")
                    if delta:
                        streamed_content += delta
                continue
            if frame.get("type") != "res" or frame.get("id") != "a1":
                continue
            if not frame.get("ok"):
                raise RuntimeError(f"OpenClaw agent失败: {frame.get('error')}")
            payload = frame.get("payload") or {}
            if payload.get("status") in ("accepted", "in_flight"):
                continue
            result_payloads = ((payload.get("result") or {}).get("payloads") or [])
            texts = [str(item.get("text") or "").strip()
                     for item in result_payloads if isinstance(item, dict)]
            content = _clean_voice_agent_result(
                "\n".join(text for text in texts if text)
            )
            if payload.get("status") != "ok" or not content:
                raise RuntimeError(
                    f"OpenClaw agent未成功: status={payload.get('status')}, "
                    f"summary={payload.get('summary')}, reply={content[:200]}"
                )
            if not _web_query_has_observed_tool(route, observed_tool_trace):
                raise RuntimeError("web_query_missing_tool_evidence")
            yield _sse_event({
                "id": "chatcmpl-jarvis", "object": "chat.completion.chunk",
                "created": 0, "model": model_id,
            }, {"content": content})
            yield _sse_event({
                "id": "chatcmpl-jarvis", "object": "chat.completion.chunk",
                "created": 0, "model": model_id,
            }, {}, "stop")
            yield "data: [DONE]\n\n"
            break

    finally:
        await ws.close()


def run_external_home_query(text: str) -> str:
    """Run the deterministic local External home backend camera adapter."""
    return default_run_home(text)


def _execute_quick_tool(plan: ExecutionPlan, text: str) -> str:
    if plan.executor == "local_4b_device":
        return control_device(text)
    if plan.executor == "local_4b_scene":
        return trigger_ac_sleep_scene(text)
    if plan.executor == "bridge_recipe":
        if plan.bundle is None:
            raise CapabilityValidationError("quick tool bundle is missing")
        return recipe_runtime.execute(plan.bundle)
    raise CapabilityValidationError("unsupported quick tool executor")


async def _recipe_stream(base: dict[str, Any], plan: ExecutionPlan, text: str):
    content = await asyncio.to_thread(_execute_quick_tool, plan, text)
    yield _sse_event(base, {"role": "assistant"})
    yield _sse_event(base, {"content": content})
    yield _sse_event(base, {}, "stop")
    yield "data: [DONE]\n\n"


async def _capability_stream(
    body: dict[str, Any], base: dict[str, Any], decision: RouteDecision,
    plan: ExecutionPlan, plan_state: dict[str, ExecutionPlan],
):
    try:
        async for event in _recipe_stream(base, plan, extract_user_text(body)):
            yield event
        return
    except RecipeExecutionError as error:
        fallback_plan = resolve_fallback_plan(
            plan, reason=error.code,
            fallback_allowed=error.fallback_allowed,
        )
    plan_state["plan"] = fallback_plan
    stage_data = {
        "capability": plan.capability,
        "capability_version": plan.capability_version,
        "planned_executor": plan.executor,
        "escalation_reason": fallback_plan.escalation_reason,
    }
    yield _stage_event(
        base, phase=plan.phase, producer=plan.producer,
        status="failed", data=stage_data,
    )
    yield _stage_event(
        base, phase=fallback_plan.phase, producer=fallback_plan.producer,
        status="started", data=stage_data,
    )
    async for event in _openclaw_ws_stream(
        body, str(body.get("model") or "jarvis"), decision.route,
    ):
        yield event


def _web_query_has_observed_tool(
    route: Route | None, tool_trace: list[dict[str, str]],
) -> bool:
    if route is not Route.WEB_QUERY:
        return True
    return any(
        item.get("provider") == "openclaw"
        and item.get("status") == "completed"
        and bool(item.get("tool"))
        for item in tool_trace
        if isinstance(item, dict)
    )


async def run_openclaw(
    body: dict[str, Any], route: Route | None = Route.HOME,
    *, tool_trace: list[dict[str, str]] | None = None,
) -> str:
    """Collect the grounded final answer and optional safe tool stages."""
    content = ""
    observed_tool_trace: list[dict[str, str]] = []
    model_id = str(body.get("model") or "jarvis")
    async for event in _openclaw_ws_stream(body, model_id, route):
        if not isinstance(event, str) or not event.startswith("data: "):
            continue
        payload = event[6:].strip()
        if payload == "[DONE]":
            continue
        try:
            frame = json.loads(payload)
        except json.JSONDecodeError:
            continue
        jarvis = frame.get("jarvis") or {}
        if jarvis.get("event") == "stage" and jarvis.get("phase") == "tool":
            data = jarvis.get("data") or {}
            tool_stage = {
                "provider": str(jarvis.get("producer") or ""),
                "tool": str(data.get("tool") or ""),
                "status": str(jarvis.get("status") or ""),
            }
            observed_tool_trace.append(tool_stage)
            if tool_trace is not None:
                tool_trace.append(tool_stage)
        delta = ((frame.get("choices") or [{}])[0].get("delta") or {})
        content += str(delta.get("content") or "")
    if not content.strip():
        raise RuntimeError("OpenClaw returned an empty answer")
    if not _web_query_has_observed_tool(route, observed_tool_trace):
        raise RuntimeError("web_query_missing_tool_evidence")
    return content.strip()


def _executor_for(decision: RouteDecision) -> str:
    if decision.route is Route.CAMERA:
        return "external_home"
    if decision.route in {Route.HOME, Route.TASK, Route.WEB_QUERY}:
        return "openclaw"
    if decision.route is Route.LOCAL_CHAT:
        return "local_4b"
    return "native"


def _decision_from_review(review: RouteReview) -> RouteDecision:
    route = {
        "task": Route.TASK,
        "camera": Route.CAMERA,
        "home": Route.HOME,
        "web_query": Route.WEB_QUERY,
        "local_chat": Route.LOCAL_CHAT,
    }[review.route]
    risk = "read_only" if review.intent in {"list", "query", "chat", "camera_recent", "camera_live"} else "mutation"
    return RouteDecision(route, review.intent, "openclaw_feedback_review", risk)


def _review_is_currently_executable(review: RouteReview) -> bool:
    expected = {
        "camera": "external_home",
        "local_chat": "local_4b",
        "task": "openclaw",
        "home": "openclaw",
        "web_query": "openclaw",
    }
    return expected.get(review.route) == review.executor


async def _execute_decision(
    body: dict[str, Any], decision: RouteDecision,
    execution_plan: ExecutionPlan | None = None,
    tool_trace: list[dict[str, str]] | None = None,
) -> tuple[dict[str, Any], str]:
    text = extract_user_text(body)
    model = str(body.get("model") or "jarvis")
    if decision.route is Route.NATIVE:
        return openai_response("", model, "native"), ""
    if decision.rule_id == "model_unavailable":
        content = "我这次没判断清楚要走哪条路线，请再说一次。"
        return openai_response(content, model, "routing_failed"), content
    if decision.route is Route.CAMERA:
        async with _lock:
            content = await asyncio.to_thread(run_external_home_query, text)
        return openai_response(content, model, "camera"), content
    if decision.capability != "none":
        plan = execution_plan or resolve_execution_plan(decision, capability_registry)
        content = await asyncio.to_thread(_execute_quick_tool, plan, text)
        return openai_response(content, model, plan.capability), content
    if decision.route in {Route.HOME, Route.TASK, Route.WEB_QUERY}:
        content = _clean_voice_agent_result(await run_openclaw(
            body, decision.route, tool_trace=tool_trace,
        ))
        route_name = decision.route.value
        return openai_response(content, model, route_name), content
    result = await local_chat.complete(body)
    content = str(((result.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    return result, content


async def _review_turn(turn_id: str) -> tuple[dict[str, Any], EvolutionReview] | None:
    turn = feedback_ledger.get_turn(turn_id)
    if not turn:
        return None
    allowed_tools = [bundle["id"] for bundle in capability_registry.list_selectable("4b")]
    prompt = build_evolution_review_prompt(turn, allowed_tools=allowed_tools)
    raw = await asyncio.to_thread(openclaw_reviewer, prompt)
    review = parse_evolution_review(raw, allowed_tools=set(allowed_tools))
    append_evolution_review(feedback_ledger.root, turn, review)
    return turn, review


async def _complete_two_stage(body: dict[str, Any]) -> dict[str, Any]:
    text = extract_user_text(body)
    model = str(body.get("model") or "jarvis")
    session_id, execution_body = _prepare_two_stage_body(body, text)
    timing: dict[str, int] = {}
    envelope = await _arbitrate_two_stage(text, timing)
    plan = resolve_arbitration_plan(envelope, capability_registry)
    effective_plan = plan
    tool_trace: list[dict[str, str]] = []
    content = ""
    execution_started = time.perf_counter()
    try:
        if plan.execution_class == "quick_tool":
            try:
                content = await asyncio.to_thread(_execute_quick_tool, plan, text)
            except RecipeExecutionError as error:
                effective_plan = resolve_fallback_plan(
                    plan, reason=error.code,
                    fallback_allowed=error.fallback_allowed,
                )
                content = _clean_voice_agent_result(await run_openclaw(
                    execution_body, None, tool_trace=tool_trace,
                ))
            result = openai_response(content, model, plan.capability)
        elif plan.execution_class == "local_chat":
            result = await local_chat.complete(execution_body)
            content = str(
                ((result.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            )
        elif plan.execution_class == "openclaw":
            content = _clean_voice_agent_result(await run_openclaw(
                execution_body, None, tool_trace=tool_trace,
            ))
            result = openai_response(content, model, "openclaw")
        else:
            raise CapabilityValidationError("unsupported execution class")
        success = True
    except Exception:
        feedback_ledger.save_turn({
            "session_id": session_id,
            "request": text, "route": "arbitrated",
            "intent": effective_plan.execution_class,
            "executor": effective_plan.executor,
            "producer": effective_plan.producer,
            "capability": effective_plan.capability,
            "capability_version": effective_plan.capability_version,
            "escalation_reason": effective_plan.escalation_reason,
            "tool_trace": tool_trace,
            "execution_elapsed_ms": round(
                (time.perf_counter() - execution_started) * 1000
            ),
            **_arbitration_ledger_fields(envelope, effective_plan, timing),
            "answer": "", "success": False,
        })
        raise
    feedback_ledger.save_turn({
        "session_id": session_id,
        "request": text, "route": "arbitrated",
        "intent": effective_plan.execution_class,
        "executor": effective_plan.executor,
        "producer": effective_plan.producer,
        "capability": effective_plan.capability,
        "capability_version": effective_plan.capability_version,
        "escalation_reason": effective_plan.escalation_reason,
        "tool_trace": tool_trace,
        "execution_elapsed_ms": round(
            (time.perf_counter() - execution_started) * 1000
        ),
        **_arbitration_ledger_fields(envelope, effective_plan, timing),
        "answer": content, "success": success,
    })
    return result


async def _complete_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Non-streaming entry point using the same Router and backends as SSE."""
    if TWO_STAGE_ARBITRATION:
        return await _complete_two_stage(body)
    text = extract_user_text(body)
    model = str(body.get("model") or "jarvis")
    decision = await intent_router.decide(text)
    plan = resolve_execution_plan(decision, capability_registry)
    effective_plan = plan
    tool_trace: list[dict[str, str]] = []
    try:
        try:
            result, content = await _execute_decision(
                body, decision, plan, tool_trace,
            )
        except RecipeExecutionError as error:
            effective_plan = resolve_fallback_plan(
                plan, reason=error.code,
                fallback_allowed=error.fallback_allowed,
            )
            content = _clean_voice_agent_result(await run_openclaw(
                body, decision.route, tool_trace=tool_trace,
            ))
            result = openai_response(content, model, plan.capability)
        success = True
    except Exception:
        feedback_ledger.save_turn({
            "session_id": body.get("session_id"),
            "request": text, "route": decision.route.value,
            "intent": decision.intent, "executor": effective_plan.executor,
            "producer": effective_plan.producer, "capability": effective_plan.capability,
            "capability_version": effective_plan.capability_version,
            "escalation_reason": effective_plan.escalation_reason,
            "tool_trace": tool_trace,
            "answer": "", "success": False,
        })
        raise
    feedback_ledger.save_turn({
        "session_id": body.get("session_id"),
        "request": text, "route": decision.route.value,
        "intent": decision.intent, "executor": effective_plan.executor,
        "producer": effective_plan.producer, "capability": effective_plan.capability,
        "capability_version": effective_plan.capability_version,
        "escalation_reason": effective_plan.escalation_reason,
        "tool_trace": tool_trace,
        "answer": content, "success": success,
    })
    return result


async def _two_stage_stream(body: dict[str, Any], base: dict[str, Any]):
    user_text = extract_user_text(body)
    session_id, execution_body = _prepare_two_stage_body(body, user_text)
    timing: dict[str, int] = {}
    l1_started = time.perf_counter()
    envelope = await level1_arbitrator.decide(user_text)
    timing["level1_elapsed_ms"] = round((time.perf_counter() - l1_started) * 1000)
    l1_data = {
        "arbitration_id": envelope.arbitration_id,
        "decision": envelope.level1.decision,
        "quick_tool_id": envelope.level1.quick_tool_id,
        "handoff": envelope.level1.handoff,
        "confidence": envelope.level1.confidence,
    }
    yield _stage_event(
        base, phase="arbitration_l1", producer="router_0_8b",
        status="completed", data=l1_data,
    )
    if envelope.level2_required:
        l2_started = time.perf_counter()
        level2_pending = asyncio.create_task(
            level2_arbitrator.decide(user_text, envelope)
        )
        transition = {
            "lookup": "好的主人，我查一下。",
            "analyze": "好的主人，我来分析一下。",
            "action": "好的主人，我来处理，执行前会确认。",
            "general": "好的主人，我来处理一下。",
        }.get(envelope.level1.handoff, "")
        if transition:
            yield _sse_event(base, {"content": transition})
            yield _sse_event(base, {})
        yield _stage_event(
            base, phase="arbitration_l2", producer="router_4b",
            status="started", data={"arbitration_id": envelope.arbitration_id},
        )
        envelope = await level2_pending
        timing["level2_elapsed_ms"] = round((time.perf_counter() - l2_started) * 1000)
        if envelope.level2 is None:
            raise CapabilityValidationError("level2 result is missing")
        yield _stage_event(
            base, phase="arbitration_l2", producer="router_4b",
            status="completed", data={
                "arbitration_id": envelope.arbitration_id,
                "decision": envelope.level2.decision,
                "quick_tool_id": envelope.level2.quick_tool_id,
                "relation": envelope.level2.relation,
                "confidence": envelope.level2.confidence,
            },
        )
    plan = resolve_arbitration_plan(envelope, capability_registry)

    compatibility_decision = RouteDecision(
        Route.LOCAL_CHAT, "chat", "two_stage_arbitration", "none", 1.0,
        capability=plan.capability,
    )
    public_plan = {
        "arbitration_id": envelope.arbitration_id,
        "capability": plan.capability,
        "capability_version": plan.capability_version,
        "planned_executor": plan.execution_class,
        "execution_class": plan.execution_class,
        "tool_class": plan.tool_class,
        "execution_kind": plan.tool_class,
        "selector_tier": plan.selector_tier,
        "risk_class": plan.risk_class,
        "decision_tier": plan.decision_tier,
    }

    if plan.execution_class == "quick_tool":
        yield _stage_event(
            base, phase="execution", producer=plan.producer,
            status="started", data=public_plan,
        )
        plan_state = {"plan": plan}
        source = _recording_stream(
            _capability_stream(
                execution_body, base, compatibility_decision, plan, plan_state,
            ),
            request=user_text, decision=compatibility_decision,
            session_id=session_id, execution_plan=plan,
            execution_plan_state=plan_state, arbitration=envelope,
            audit_route="arbitrated", audit_intent=plan.execution_class,
            arbitration_timing=timing,
        )
        async for event in _with_progress(
            source, base, (plan.transition, plan.progress, plan.long_wait),
        ):
            yield event
        return

    if plan.execution_class == "local_chat":
        yield _stage_event(
            base, phase="generation", producer=plan.producer,
            status="started", data=public_plan,
        )
        source = _recording_stream(
            local_chat.stream(execution_body),
            request=user_text, decision=compatibility_decision,
            session_id=session_id, execution_plan=plan, arbitration=envelope,
            audit_route="arbitrated", audit_intent=plan.execution_class,
            arbitration_timing=timing,
        )
        async for event in _with_progress(
            source, base, (plan.transition, plan.progress, plan.long_wait),
        ):
            yield event
        return

    if plan.execution_class != "openclaw":
        raise CapabilityValidationError("unsupported execution class")
    yield _stage_event(
        base, phase="planning", producer="openclaw",
        status="started", data=public_plan,
    )
    source = _recording_stream(
        _openclaw_ws_stream(execution_body, base["model"], None),
        request=user_text, decision=compatibility_decision,
        session_id=session_id, execution_plan=plan, arbitration=envelope,
        audit_route="arbitrated", audit_intent=plan.execution_class,
        arbitration_timing=timing,
    )
    async for event in _with_progress(
        source, base, (plan.transition, plan.progress, plan.long_wait),
    ):
        yield event


async def _stream_chat(body: dict[str, Any]):
    """Route one voice turn to the single selected backend."""
    base = {
        "id": "chatcmpl-jarvis",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": str(body.get("model") or "hermes-jarvis"),
    }

    if TWO_STAGE_ARBITRATION:
        async for event in _two_stage_stream(body, base):
            yield event
        return

    model_id = base["model"]
    user_text = extract_user_text(body)
    session_id = feedback_ledger.resolve_session(body.get("session_id"))

    yield _stage_event(
        base, phase="arbitration", producer="router_0_8b", status="started",
    )
    decision = await intent_router.decide(user_text)
    yield _stage_event(
        base, phase="arbitration", producer="router_0_8b", status="completed",
        data={
            "route": decision.route.value, "operation": decision.intent,
            "confidence": decision.confidence, "risk": decision.risk,
        },
    )

    # The ledger is the trusted session source. Downstream 9B/OpenClaw receive
    # recent conversational context, while the 0.8B arbitrates only the newest request.
    history_messages: list[dict[str, str]] = []
    for turn in feedback_ledger.session_turns(session_id)[-10:]:
        history_messages.extend([
            {"role": "user", "content": str(turn["request"])},
            {"role": "assistant", "content": str(turn["answer"])},
        ])
    execution_body = dict(body)
    execution_body["session_id"] = session_id
    execution_body["messages"] = history_messages + [{"role": "user", "content": user_text}]

    # Camera/live-scene queries bypass the general OpenClaw tool loop. The
    # BridgeEngine routes these deterministically to External home backend.
    if decision.route is Route.CAMERA:
        yield _stage_event(base, phase="execution", producer="external_home", status="started")
        source = _recording_stream(
            _visual_result_stream(execution_body, base),
            request=user_text,
            decision=decision,
            session_id=session_id,
        )
        async for chunk in _with_progress(
            source,
            base,
            _progress_phrases(decision),
        ):
            yield chunk
        return

    if decision.capability != "none":
        plan = resolve_execution_plan(decision, capability_registry)
        stage_data = {
            "capability": plan.capability,
            "capability_version": plan.capability_version,
            "planned_executor": plan.executor,
        }
        yield _stage_event(
            base, phase=plan.phase, producer=plan.producer,
            status="started", data=stage_data,
        )
        plan_state = {"plan": plan}
        source = _recording_stream(
            _capability_stream(execution_body, base, decision, plan, plan_state),
            request=user_text, decision=decision, session_id=session_id,
            execution_plan=plan, execution_plan_state=plan_state,
        )
        phrases = (plan.transition, plan.progress, plan.long_wait)
        async for chunk in _with_progress(source, base, phrases):
            yield chunk
        return

    # OpenClaw's full agent/tool context is only worth the overhead for explicit
    # home/hardware and durable task intents. Normal conversation stays local.
    use_openclaw_for_request = USE_OPENCLAW and decision.route in {Route.HOME, Route.TASK, Route.WEB_QUERY}
    if use_openclaw_for_request:
        try:
            yield _stage_event(base, phase="planning", producer="openclaw", status="started")
            source = _recording_stream(
                _openclaw_ws_stream(execution_body, model_id, decision.route),
                request=user_text,
                decision=decision,
                session_id=session_id,
            )
            async for chunk in _with_progress(
                source,
                base,
                _progress_phrases(decision),
            ):
                yield chunk
            return
        except Exception as e:
            # Never let a generic text model fabricate device state or claim
            # an action succeeded after the grounded Agent failed.
            failure_text = (
                "这个任务没有创建成功，我没有保存任何任务。"
                if decision.route is Route.TASK
                else "这次没处理成功，我没有执行任何操作。"
            )
            yield _sse_event(base, {"content": failure_text})
            yield _sse_event(base, {}, "stop")
            yield "data: [DONE]\n\n"
            return

    if decision.route is Route.NATIVE:
        yield _sse_event(base, {"role": "assistant"})
        yield _sse_event(base, {}, "stop")
        yield "data: [DONE]\n\n"
        return

    if decision.rule_id == "model_unavailable":
        async def routing_failure_source():
            yield _sse_event(base, {"role": "assistant", "content": "我这次没判断清楚要走哪条路线，请再说一次。"})
            yield _sse_event(base, {}, "stop")
            yield "data: [DONE]\n\n"
        async for event in _recording_stream(
            routing_failure_source(), request=user_text, decision=decision,
            session_id=session_id, success_override=False,
        ):
            yield event
        return

    # Normal conversation goes directly from :18083 to Ollama.
    try:
        yield _stage_event(base, phase="generation", producer="local_4b", status="started")
        source = _recording_stream(
            local_chat.stream(execution_body),
            request=user_text,
            decision=decision,
            session_id=session_id,
        )
        async for event in _with_progress(
            source,
            base,
            _progress_phrases(decision),
        ):
            yield event
        return
    except Exception as error:
        yield _sse_event(base, {"content": f"本地聊天暂时不可用：{error}"}, "stop")
        yield "data: [DONE]\n\n"


def _authorized(request: Request) -> bool:
    if not _api_key:
        return False
    supplied = request.headers.get("Authorization", "")
    expected = f"Bearer {_api_key}"
    return hmac.compare_digest(supplied, expected)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok", "profile": "jarvis",
        "openclaw_ws": OPENCLAW_WS,
        "local_chat": local_chat.url,
        "use_openclaw": USE_OPENCLAW,
    }


@app.get("/evolution", response_class=HTMLResponse)
async def evolution_dashboard() -> HTMLResponse:
    try:
        content = _EVOLUTION_DASHBOARD.read_text(encoding="utf-8")
    except OSError:
        return HTMLResponse("Evolution Dashboard is not installed.", status_code=503)
    return HTMLResponse(content, headers={"Cache-Control": "no-store"})


@app.get("/v1/evolution/dashboard")
async def evolution_dashboard_data(request: Request):
    if not _authorized(request):
        return JSONResponse(
            {"error": {"message": "Unauthorized", "type": "authentication_error"}},
            status_code=401,
        )
    try:
        return JSONResponse(_evolution_dashboard_payload())
    except (CapabilityValidationError, OSError, TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "server_error"}}, status_code=500,
        )


@app.post("/v1/evolution/capabilities/{capability_id}/tier")
async def propose_manual_tier(capability_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse(
            {"error": {"message": "Unauthorized", "type": "authentication_error"}},
            status_code=401,
        )
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"target_tier", "reason"}:
            raise CapabilityValidationError("invalid manual tier request")
        target_tier = str(body.get("target_tier") or "").strip()
        reason = str(body.get("reason") or "").strip()
        if target_tier not in _TIER_ORDER or not 2 <= len(reason) <= 240:
            raise CapabilityValidationError("target tier or reason is invalid")
        active = capability_registry.active(capability_id)
        if active is None:
            raise CapabilityValidationError("active capability not found")
        from_tier = str(active["selector"]["tier"])
        if from_tier not in _TIER_ORDER:
            raise CapabilityValidationError("active capability has an invalid tier")
        if abs(_TIER_ORDER.index(target_tier) - _TIER_ORDER.index(from_tier)) != 1:
            raise CapabilityValidationError("manual tier transition must be adjacent")
        audit_id = "manual-" + hashlib.sha256(
            f"{capability_id}\0{from_tier}\0{target_tier}\0{reason}\0{time.time_ns()}".encode()
        ).hexdigest()[:20]
        proposal = capability_registry.propose_tier_change(
            capability_id, target_tier, [audit_id],
        )
        _append_evolution_audit({
            "schema_version": 1, "timestamp": _utc_timestamp(),
            "event": "tier_proposed", "audit_id": audit_id,
            "actor": "evolution_dashboard", "capability_id": capability_id,
            "from_tier": from_tier, "target_tier": target_tier,
            "from_version": int(active["version"]),
            "candidate_version": int(proposal["capability"]["version"]),
            "reason": reason,
        })
        return JSONResponse({
            "status": "proposed", "audit_id": audit_id,
            "candidate": proposal,
        })
    except (CapabilityValidationError, TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )


@app.delete("/v1/evolution/capabilities/{capability_id}/candidate")
async def discard_manual_candidate(capability_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse(
            {"error": {"message": "Unauthorized", "type": "authentication_error"}},
            status_code=401,
        )
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"reason"}:
            raise CapabilityValidationError("invalid discard request")
        reason = str(body.get("reason") or "").strip()
        if not 2 <= len(reason) <= 240:
            raise CapabilityValidationError("discard reason is invalid")
        discarded = capability_registry.discard_candidate(capability_id)
        bundle = discarded.get("capability") if isinstance(discarded, dict) else {}
        _append_evolution_audit({
            "schema_version": 1, "timestamp": _utc_timestamp(),
            "event": "candidate_discarded", "actor": "evolution_dashboard",
            "capability_id": capability_id,
            "candidate_version": int(bundle.get("version") or 0),
            "target_tier": str((bundle.get("selector") or {}).get("tier") or ""),
            "reason": reason,
        })
        return JSONResponse({"status": "discarded"})
    except (CapabilityValidationError, TypeError, ValueError) as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": "hermes-jarvis", "object": "model", "owned_by": "local"}]}


@app.post("/v1/chat/completions")
async def chat(request: Request):
    if not _authorized(request):
        return JSONResponse(
            {"error": {"message": "Unauthorized", "type": "authentication_error"}},
            status_code=401,
        )
    try:
        body = await request.json()
        if body.get("stream") is True:
            return StreamingResponse(
                _stream_chat(body),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return JSONResponse(await _complete_chat(body))
    except ValueError as exc:
        return JSONResponse({"error": {"message": str(exc), "type": "invalid_request_error"}}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": {"message": str(exc), "type": "server_error"}}, status_code=500)


@app.get("/v1/sessions")
async def sessions(request: Request, limit: int = 50):
    if not _authorized(request):
        return JSONResponse(
            {"error": {"message": "Unauthorized", "type": "authentication_error"}},
            status_code=401,
        )
    return JSONResponse({"sessions": feedback_ledger.list_sessions(limit)})


@app.get("/v1/sessions/{session_id}/turns")
async def session_history(session_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse(
            {"error": {"message": "Unauthorized", "type": "authentication_error"}},
            status_code=401,
        )
    turns = feedback_ledger.session_turns(session_id)
    if not turns:
        return JSONResponse({"error": {"message": "session not found", "type": "not_found"}}, status_code=404)
    return JSONResponse({"session_id": session_id, "turns": turns})


@app.get("/v1/turns")
async def turn_history(request: Request, limit: int = 50):
    if not _authorized(request):
        return JSONResponse(
            {"error": {"message": "Unauthorized", "type": "authentication_error"}},
            status_code=401,
        )
    return JSONResponse({"turns": feedback_ledger.list_turns(limit)})


@app.get("/v1/capabilities")
async def capabilities(request: Request):
    if not _authorized(request):
        return JSONResponse({"error": {"message": "Unauthorized", "type": "authentication_error"}}, status_code=401)
    active = []
    for bundle in capability_registry.list_active():
        if not isinstance(bundle, dict):
            continue
        execution = bundle.get("execution") or {}
        permissions = bundle.get("permissions") or {}
        selector = bundle.get("selector") or {}
        taxonomy = bundle.get("taxonomy") or {}
        active.append({
            "id": str(bundle.get("id") or ""),
            "version": int(bundle.get("version") or 0),
            "status": str(bundle.get("status") or "active"),
            "active_in_production": True,
            # Preserve the pre-feature contract; execution_class is the new
            # product-level classification and must not replace this field.
            "planned_executor": str(execution.get("primary") or ""),
            "execution_class": (
                "openclaw"
                if execution.get("primary") == "openclaw"
                else "quick_tool"
            ),
            "producer": str(execution.get("producer") or ""),
            "fallback": str(execution.get("fallback") or ""),
            "risk": str(permissions.get("risk") or ""),
            "selector_tier": str(selector.get("tier") or "openclaw_only"),
            "selector_description": str(selector.get("description") or ""),
            "domain_tags": [
                str(tag) for tag in (taxonomy.get("domain_tags") or [])
            ],
            "execution_kind": str(taxonomy.get("execution_kind") or ""),
        })
    candidates = []
    for proposal in capability_registry.list_candidates():
        bundle = proposal.get("capability") if isinstance(proposal, dict) else None
        if not isinstance(bundle, dict):
            continue
        selector = bundle.get("selector") or {}
        taxonomy = bundle.get("taxonomy") or {}
        permissions = bundle.get("permissions") or {}
        candidates.append({
            "id": str(bundle.get("id") or ""),
            "version": int(bundle.get("version") or 0),
            "from_version": int(proposal.get("from_version") or 0),
            "status": str(proposal.get("status") or "proposed"),
            "active_in_production": False,
            "selector_tier": str(selector.get("tier") or "openclaw_only"),
            "selector_description": str(selector.get("description") or ""),
            "domain_tags": [
                str(tag) for tag in (taxonomy.get("domain_tags") or [])
            ],
            "execution_kind": str(taxonomy.get("execution_kind") or ""),
            "risk": str(permissions.get("risk") or ""),
        })
    return JSONResponse({
        "active": active,
        "candidates": candidates,
    })


@app.post("/v1/capabilities/{capability_id}/validate")
async def validate_capability(capability_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": {"message": "Unauthorized", "type": "authentication_error"}}, status_code=401)
    try:
        body = await request.json()
        if body != {}:
            raise CapabilityValidationError("validation body must be empty")
        report = _capability_validator().validate_candidate(capability_id)
        _append_evolution_audit({
            "schema_version": 1, "timestamp": _utc_timestamp(),
            "event": "candidate_validated", "actor": "evolution_dashboard_or_api",
            "capability_id": capability_id, "report_hash": report["report_hash"],
            "passed": report["passed"],
        })
        response = {
            "status": "validated",
            "report_hash": report["report_hash"],
            "passed": report["passed"],
            "gates": {
                str(name): gate.get("passed") is True
                for name, gate in report["gates"].items()
                if isinstance(gate, dict)
            },
        }
        if report.get("schema_version") == 2:
            response.update({
                "change_kind": str(report.get("change_kind") or ""),
                "from_tier": str(report.get("from_tier") or ""),
                "target_tier": str(report.get("target_tier") or ""),
            })
        return JSONResponse(response)
    except (CapabilityValidationError, TypeError, ValueError) as exc:
        return JSONResponse({"error": {"message": str(exc), "type": "invalid_request_error"}}, status_code=400)


@app.post("/v1/capabilities/{capability_id}/promote")
async def promote_capability(capability_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": {"message": "Unauthorized", "type": "authentication_error"}}, status_code=401)
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"expected_version", "report_hash"}:
            raise CapabilityValidationError("invalid promotion request")
        report = _capability_validator().report(str(body["report_hash"]))
        if report is None:
            raise CapabilityValidationError("validation report not found")
        previous = capability_registry.active(capability_id)
        active = capability_registry.promote_with_report(
            capability_id, int(body["expected_version"]), report,
        )
        _append_evolution_audit({
            "schema_version": 1, "timestamp": _utc_timestamp(),
            "event": "candidate_promoted", "actor": "evolution_dashboard_or_api",
            "capability_id": capability_id,
            "from_version": int(previous["version"]) if previous else 0,
            "to_version": int(active["version"]),
            "from_tier": str(previous["selector"]["tier"]) if previous else "",
            "target_tier": str(active["selector"]["tier"]),
            "report_hash": str(body["report_hash"]),
        })
        return JSONResponse({"status": "promoted", "active": active})
    except (CapabilityValidationError, TypeError, ValueError) as exc:
        return JSONResponse({"error": {"message": str(exc), "type": "invalid_request_error"}}, status_code=400)


@app.post("/v1/capabilities/{capability_id}/rollback")
async def rollback_capability(capability_id: str, request: Request):
    if not _authorized(request):
        return JSONResponse({"error": {"message": "Unauthorized", "type": "authentication_error"}}, status_code=401)
    try:
        body = await request.json()
        if body != {}:
            raise CapabilityValidationError("rollback body must be empty")
        previous = capability_registry.active(capability_id)
        active = capability_registry.rollback(capability_id)
        _append_evolution_audit({
            "schema_version": 1, "timestamp": _utc_timestamp(),
            "event": "capability_rolled_back", "actor": "evolution_dashboard_or_api",
            "capability_id": capability_id,
            "from_version": int(previous["version"]) if previous else 0,
            "to_version": int(active["version"]),
            "from_tier": str(previous["selector"]["tier"]) if previous else "",
            "target_tier": str(active["selector"]["tier"]),
        })
        return JSONResponse({"status": "rolled_back", "active": active})
    except (CapabilityValidationError, ValueError) as exc:
        return JSONResponse({"error": {"message": str(exc), "type": "invalid_request_error"}}, status_code=400)


@app.post("/v1/route-reviews")
async def route_review(request: Request):
    if not _authorized(request):
        return JSONResponse(
            {"error": {"message": "Unauthorized", "type": "authentication_error"}},
            status_code=401,
        )
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"turn_id"}:
            raise ValueError("body must contain exactly turn_id")
        turn_id = str(body.get("turn_id") or "").strip()
        if not turn_id or len(turn_id) > 128:
            raise ValueError("invalid turn_id")
        reviewed = await _review_turn(turn_id)
        if not reviewed:
            return JSONResponse(
                {"error": {"message": "turn not found", "type": "not_found"}},
                status_code=404,
            )
        turn, review = reviewed
        original_l1 = turn.get("level1") if isinstance(turn.get("level1"), dict) else {}
        original_l2 = turn.get("level2") if isinstance(turn.get("level2"), dict) else None
        expected_l1 = {
            "decision": str(original_l1.get("decision") or ""),
            "quick_tool_id": str(original_l1.get("quick_tool_id") or "none"),
            "handoff": str(original_l1.get("handoff") or "none"),
        }
        expected_l2 = None if original_l2 is None else {
            "decision": str(original_l2.get("decision") or ""),
            "quick_tool_id": str(original_l2.get("quick_tool_id") or "none"),
        }
        candidate_created = expected_l1 != review.level1 or expected_l2 != review.level2
        return JSONResponse({
            "status": "reviewed",
            "candidate_created": candidate_created,
            "capability_candidate": None,
            "turn_id": turn["turn_id"],
            "review": {
                "level1": review.level1,
                "level2": review.level2,
                "confidence": review.confidence,
                "reason": review.reason,
            },
            "auto_replayed": False,
        })
    except ValueError as exc:
        return JSONResponse({"error": {"message": str(exc), "type": "invalid_request_error"}}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": {"message": str(exc), "type": "server_error"}}, status_code=500)
