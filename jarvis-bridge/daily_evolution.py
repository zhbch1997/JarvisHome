"""Daily gated evolution for the 0.8B router and executor capability boundary."""
from __future__ import annotations

import json
import os
import statistics
import time
import urllib.request
from pathlib import Path
from typing import Any

from capability_evolution import propose_from_review
from capability_registry import CapabilityRegistry
from feedback_loop import parse_review
from prompt_evolution import PromptEvolution
from semantic_router import _OUTPUT_SCHEMA, build_router_prompt

BASELINE = [
    ("每天晚上九点提醒我看看宠物仓鼠的饮水器", "task"),
    ("帮我盯着孩子每天练琴，满30分钟提醒我", "task"),
    ("提醒家里老人每天量一次血压", "task"),
    ("家里没人时如果窗户还开着就通知我", "task"),
    ("宠物仓鼠在干嘛", "camera_recent"),
    ("看看乌龟最近有没有晒背", "camera_recent"),
    ("现在实时看看宠物仓鼠", "camera_live"),
    ("重新打开摄像头看一下乌龟", "camera_live"),
    ("打开示例房间的灯", "home"),
    ("查询空调现在多少度", "home"),
    ("讲个笑话", "chat"),
    ("为什么空调能制冷", "chat"),
]


def _valid_router_output(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"intent", "operation", "capability", "confidence"}:
        return False
    properties = _OUTPUT_SCHEMA["properties"]
    return (
        value["intent"] in properties["intent"]["enum"]
        and value["operation"] in properties["operation"]["enum"]
        and value["capability"] in properties["capability"]["enum"]
        and isinstance(value["confidence"], (int, float))
        and not isinstance(value["confidence"], bool)
        and 0 <= value["confidence"] <= 1
    )


def _model_intent(item: dict[str, str]) -> str:
    if item["intent"] in {"camera_recent", "camera_live"}:
        return item["intent"]
    if item["route"] == "local_chat":
        return "chat"
    return item["route"]


def ollama_evaluator(examples: list[dict[str, str]]) -> dict[str, float]:
    state = Path(os.getenv("JARVIS_FEEDBACK_STATE", "./state/route-feedback"))
    capability_registry = CapabilityRegistry(
        Path(os.getenv("JARVIS_CAPABILITY_STATE", "./state/capabilities"))
    )
    candidate_prompt_path = state / ".candidate-eval.json"
    candidate_prompt_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_prompt_path.write_text(json.dumps(examples, ensure_ascii=False), encoding="utf-8")
    candidate_prompt_path.chmod(0o600)
    samples = [(text, intent, None) for text, intent in BASELINE] + [
        (item["request"], _model_intent(item), item["intent"] if item["intent"] in {"create", "list", "update", "delete", "query", "action", "chat"} else "query")
        for item in examples
    ]
    results: list[tuple[str, str | None, str | None, str | None, bool, float]] = []
    try:
        system = build_router_prompt(
            candidate_prompt_path,
            capability_registry=capability_registry,
        )
        for text, expected, expected_operation in samples:
            payload = json.dumps({
                "model": os.getenv("JARVIS_ROUTER_MODEL", "qwen35-router:0.8b"),
                "system": system, "prompt": text, "stream": False,
                "format": _OUTPUT_SCHEMA, "think": False, "keep_alive": "30m",
                "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 40},
            }, ensure_ascii=False).encode()
            started = time.monotonic()
            request = urllib.request.Request(
                os.getenv("JARVIS_ROUTER_URL", "http://127.0.0.1:11435/api/generate"),
                data=payload, headers={"Content-Type": "application/json"},
            )
            valid = False
            predicted = None
            operation = None
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    outer = json.load(response)
                parsed = json.loads(str(outer.get("response") or ""))
                predicted = parsed.get("intent")
                operation = parsed.get("operation")
                valid = _valid_router_output(parsed)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            results.append((expected, predicted, expected_operation, operation, valid, time.monotonic() - started))
    finally:
        candidate_prompt_path.unlink(missing_ok=True)
    baseline = results[:len(BASELINE)]
    candidate = results[len(BASELINE):]
    latencies = sorted(item[5] for item in results)
    p95_index = max(0, min(len(latencies) - 1, int(len(latencies) * 0.95) - 1))
    return {
        "valid_json_rate": sum(item[4] for item in results) / len(results),
        "baseline_accuracy": sum(a == b for a, b, _, _, _, _ in baseline) / len(baseline),
        "candidate_accuracy": (
            sum(a == b and (op_expected is None or op_expected == op) for a, b, op_expected, op, _, _ in candidate) / len(candidate)
        ) if candidate else 1.0,
        "p50_seconds": statistics.median(item[5] for item in results),
        "p95_seconds": latencies[p95_index],
    }


def propose_feedback_capabilities(
    feedback_path: Path, registry: CapabilityRegistry,
) -> list[dict[str, Any]]:
    try:
        rows = [
            json.loads(line)
            for line in feedback_path.read_text(encoding="utf-8").splitlines()
        ]
    except OSError:
        rows = []
    proposals: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            review = parse_review(json.dumps({
                "route": row["review_route"],
                "intent": row["review_intent"],
                "executor": row["review_executor"],
                "capability": row.get("review_capability") or "none",
                "confidence": row["review_confidence"],
                "safe_to_retry": row["review_safe_to_retry"],
                "reason": row["review_reason"],
            }, ensure_ascii=False))
        except (KeyError, TypeError, ValueError):
            continue
        turn = {
            "turn_id": str(row.get("original_turn_id") or ""),
            "request": str(row.get("request") or ""),
        }
        proposal = propose_from_review(registry, turn, review)
        if proposal is not None:
            proposals[review.capability] = proposal
    return list(proposals.values())


def run_daily(root: Path, evaluator=ollama_evaluator, *, capability_registry=None) -> dict[str, Any]:
    root = Path(root)
    prompt = PromptEvolution(root)
    promotion = prompt.promote_if_safe(evaluator)
    registry = capability_registry or CapabilityRegistry(
        Path(os.getenv("JARVIS_CAPABILITY_STATE", "./state/capabilities"))
    )
    capabilities = propose_feedback_capabilities(root / "feedback.jsonl", registry)
    return {"status": "ok", "prompt": promotion, "capability_candidate_count": len(capabilities)}
