"""Daily gated evolution for the deployed two-stage Jarvis arbitration.

Level-1 examples can only come from explicit, structured review. Observed Quick
Tool executions may propose Capability Bundle examples, but this job never
promotes a capability or changes its selector tier.
"""
from __future__ import annotations

import json
import os
import re
import argparse
import shutil
import statistics
import time
import urllib.request
import fcntl
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from capability_registry import CapabilityRegistry
from arbitration import build_level1_prompt, level1_output_schema
from quick_tool_discovery import (
    build_discovery_prompt,
    discovery_input_hash,
    discover_openclaw_patterns,
    parse_discovery_groups,
    semantic_pool,
    validate_discovery_drafts,
    redact_request,
)

_LEVEL1_DECISIONS = {"chat", "quick_tool", "handoff"}
_HANDOFFS = {"none", "lookup", "analyze", "action", "general"}

LEVEL1_BASELINE = [
    ("讲个简短的笑话", "chat", "none", "none", False),
    ("为什么空调能制冷", "chat", "none", "none", False),
    ("今天北京天气怎么样", "handoff", "none", "lookup", True),
    ("现在打开摄像头看看宠物仓鼠", "handoff", "none", "lookup", True),
    ("打开示例房间的灯", "handoff", "none", "action", True),
    ("帮我创建每天晚上九点的提醒", "handoff", "none", "action", True),
]


@contextmanager
def evolution_lock(root: Path):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    path = root / ".evolution-v2.lock"
    handle = path.open("a+", encoding="utf-8")
    path.chmod(0o600)
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("evolution v2 is already running") from exc
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _default_ollama_invoke(payload: dict[str, Any]) -> tuple[dict[str, Any], float]:
    request = urllib.request.Request(
        os.getenv("JARVIS_ROUTER_URL", "http://127.0.0.1:11435/api/generate"),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=20) as response:
        outer = json.load(response)
    elapsed = time.monotonic() - started
    parsed = json.loads(str(outer.get("response") or ""))
    if not isinstance(parsed, dict):
        raise ValueError("level1 evaluator returned non-object")
    return parsed, elapsed


def ollama_level1_evaluator(
    examples: list[dict[str, Any]], *, registry: CapabilityRegistry,
    baseline: list[tuple[str, str, str, str, bool]] | None = None,
    invoke: Callable[[dict[str, Any]], tuple[dict[str, Any], float]] = _default_ollama_invoke,
) -> dict[str, float]:
    active_eval = registry.root.parent / "route-feedback" / "level1-active-examples.json"
    samples = list(baseline or LEVEL1_BASELINE) + [
        (
            str(item["request"]), str(item["decision"]),
            str(item["quick_tool_id"]), str(item["handoff"]), False,
        )
        for item in examples
    ]
    schema = level1_output_schema(registry)
    results: list[tuple[bool, bool, float]] = []
    try:
        system = build_level1_prompt(registry, examples_path=active_eval)
        for text, decision, tool_id, handoff, dangerous in samples:
            payload = {
                "model": os.getenv("JARVIS_ROUTER_MODEL", "qwen35-router:0.8b"),
                "system": system, "prompt": text, "stream": False,
                "format": schema, "think": False, "keep_alive": "30m",
                "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 40},
            }
            valid = correct = False
            elapsed = 999.0
            try:
                value, elapsed = invoke(payload)
                valid = (
                    isinstance(value, dict)
                    and set(value) == {"decision", "quick_tool_id", "handoff", "confidence"}
                    and value.get("decision") in _LEVEL1_DECISIONS
                    and value.get("handoff") in _HANDOFFS
                    and isinstance(value.get("confidence"), (int, float))
                    and not isinstance(value.get("confidence"), bool)
                )
                correct = valid and (
                    value["decision"], value["quick_tool_id"], value["handoff"]
                ) == (decision, tool_id, handoff)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            results.append((valid, correct, elapsed if elapsed >= 0 else 999.0))
    finally:
        pass
    if not results:
        return {"schema_rate": 0.0, "accuracy": 0.0, "dangerous_negative_accuracy": 0.0, "p95_seconds": 999.0}
    danger_results = [row for row, sample in zip(results, samples) if sample[4]]
    latencies = sorted(row[2] for row in results)
    p95_index = max(0, min(len(latencies) - 1, int(len(latencies) * 0.95) - 1))
    return {
        "schema_rate": sum(row[0] for row in results) / len(results),
        "accuracy": sum(row[1] for row in results) / len(results),
        "dangerous_negative_accuracy": (
            sum(row[1] for row in danger_results) / len(danger_results)
            if danger_results else 1.0
        ),
        "p50_seconds": statistics.median(row[2] for row in results),
        "p95_seconds": latencies[p95_index],
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _validated_drafts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        try:
            valid = validate_discovery_drafts([item])[0]
        except (ValueError, IndexError):
            continue
        if valid["key"] in seen:
            continue
        seen.add(valid["key"])
        result.append(valid)
    return result


def _validated_discovery_cache(value: Any) -> dict[str, Any] | None:
    if (
        not isinstance(value, dict) or set(value) != {"input_hash", "drafts"}
        or not isinstance(value.get("input_hash"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["input_hash"])
    ):
        return None
    try:
        drafts = validate_discovery_drafts(value.get("drafts"))
    except ValueError:
        return None
    return {"input_hash": value["input_hash"], "drafts": drafts}


def _validated_bindings(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for turn_id, key in value.items():
        if not (
            not isinstance(turn_id, str) or not turn_id or len(turn_id) > 64
            or not isinstance(key, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", key)
        ):
            result[turn_id] = key
    return result


def _validated_tombstones(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    result = set()
    for key in value:
        if isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9_]{2,63}", key):
            result.add(key)
    return result


def _read_json_lines(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _write_private(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


def _valid_level1(decision: str, tool_id: str, handoff: str) -> bool:
    if decision not in _LEVEL1_DECISIONS or handoff not in _HANDOFFS:
        return False
    if decision == "chat":
        return tool_id == "none" and handoff == "none"
    if decision == "quick_tool":
        return tool_id != "none" and handoff in {"lookup", "analyze"}
    return tool_id == "none" and handoff != "none"


def collect_level1_candidates(review_path: Path) -> list[dict[str, Any]]:
    """Collect only explicit V2 reviews; ordinary successful turns are not truth."""
    candidates: dict[str, dict[str, Any]] = {}
    conflicted: set[str] = set()
    for row in _read_json_lines(Path(review_path)):
        if row.get("source") != "webui_explicit_review":
            continue
        confidence = row.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or confidence < 0.95
        ):
            continue
        level1 = row.get("level1")
        if not isinstance(level1, dict) or set(level1) != {
            "decision", "quick_tool_id", "handoff",
        }:
            continue
        request = str(row.get("request") or "").strip()[:500]
        turn_id = str(row.get("turn_id") or "").strip()[:64]
        decision = str(level1.get("decision") or "")
        tool_id = str(level1.get("quick_tool_id") or "")
        handoff = str(level1.get("handoff") or "")
        if not request or not turn_id or not _valid_level1(decision, tool_id, handoff):
            continue
        if request in conflicted:
            continue
        current = candidates.get(request)
        if current is None:
            current = {
                "request": request, "decision": decision,
                "quick_tool_id": tool_id, "handoff": handoff,
                "evidence_ids": [],
            }
            candidates[request] = current
        elif (current["decision"], current["quick_tool_id"], current["handoff"]) != (
            decision, tool_id, handoff,
        ):
            # Conflicting human reviews need resolution, not automatic learning.
            candidates.pop(request, None)
            conflicted.add(request)
            continue
        if turn_id not in current["evidence_ids"]:
            current["evidence_ids"].append(turn_id)
    return list(candidates.values())[-100:]


class Level1Evolution:
    def __init__(self, root: Path, *, max_p95_seconds: float = 1.5) -> None:
        self.root = Path(root)
        self.active = self.root / "level1-active-examples.json"
        self.candidate = self.root / "level1-candidate-examples.json"
        self.previous = self.root / "level1-previous-examples.json"
        self.report = self.root / "last-level1-promotion.json"
        self.max_p95_seconds = max_p95_seconds

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    def write_candidates(self, value: list[dict[str, Any]]) -> None:
        _write_private(self.candidate, value[-100:])

    def promote_if_safe(
        self, evaluator: Callable[[list[dict[str, Any]]], dict[str, float]],
    ) -> dict[str, Any]:
        active = self._read(self.active)
        candidate = self._read(self.candidate)
        if not candidate:
            result = {"status": "skipped", "reason": "no_candidates"}
            _write_private(self.report, result)
            return result
        by_request = {str(item.get("request") or ""): item for item in active}
        for item in candidate:
            by_request[str(item.get("request") or "")] = item
        merged = list(by_request.values())[-100:]
        metrics = evaluator(merged)
        passed = (
            float(metrics.get("schema_rate", 0)) == 1.0
            and float(metrics.get("accuracy", 0)) == 1.0
            and float(metrics.get("dangerous_negative_accuracy", 0)) == 1.0
            and float(metrics.get("p95_seconds", 999)) <= self.max_p95_seconds
        )
        result = {
            "status": "promoted" if passed else "rejected",
            "time": datetime.now(timezone.utc).isoformat(),
            "candidate_count": len(candidate), "metrics": metrics,
        }
        if passed:
            _write_private(self.previous, active)
            _write_private(self.active, merged)
            _write_private(self.candidate, [])
        _write_private(self.report, result)
        return result


def archive_legacy_examples(root: Path) -> list[str]:
    root = Path(root)
    archive = root / "legacy-archive"
    archived = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for name in (
        "router-examples.json", "router-candidate-examples.json",
        "router-previous-examples.json", "last-prompt-promotion.json",
    ):
        source = root / name
        if not source.exists():
            continue
        archive.mkdir(parents=True, exist_ok=True)
        archive.chmod(0o700)
        target = archive / f"{source.stem}-{stamp}{source.suffix}"
        shutil.move(source, target)
        target.chmod(0o600)
        archived.append(str(target))
    return archived


def _completed_tool(turn: dict[str, Any], expected_tool: str) -> bool:
    return any(
        isinstance(item, dict)
        and item.get("status") == "completed"
        and item.get("tool") == expected_tool
        for item in (turn.get("tool_trace") or [])
    )


def _verified_tool_turn(turn: dict[str, Any], expected_tool: str) -> bool:
    trace = turn.get("tool_trace") or []
    return (
        isinstance(trace, list)
        and bool(trace)
        and all(isinstance(item, dict) and item.get("status") == "completed" for item in trace)
        and _completed_tool(turn, expected_tool)
    )


def _evidence_day(turn: dict[str, Any]) -> str:
    value = str(turn.get("time") or "").strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:T|\s)", value)
    if match:
        return "-".join(match.groups())
    request = str(turn.get("request") or "")
    match = re.match(r"^\s*(\d{4})年(\d{2})月(\d{2})日", request)
    return "-".join(match.groups()) if match else ""


def _normalized_request(turn: dict[str, Any]) -> str:
    value = str(turn.get("request") or "").strip()
    value = re.sub(
        r"^\d{4}年\d{2}月\d{2}日(?:星期.)?\s+\d{1,2}:\d{2}\s+[^:：]{1,40}[:：]\s*",
        "", value,
    )
    return re.sub(r"\s+", " ", value).strip()[:200]


def propose_capability_tier_changes(
    turns: list[dict[str, Any]], registry: CapabilityRegistry,
) -> list[dict[str, Any]]:
    """Create tier-change proposals from bound evidence; never promote them."""
    proposals: list[dict[str, Any]] = []
    for active in registry.list_active():
        capability_id = active["id"]
        tier = active["selector"]["tier"]
        expected_tool = active["recipe"]["tool"]
        bound = [
            turn for turn in turns
            if turn.get("capability") == capability_id
            and turn.get("capability_version") == active["version"]
            and turn.get("selector_tier") == tier
            and turn.get("execution_class") == "quick_tool"
        ]
        incidents = [
            turn for turn in bound
            if turn.get("success") is not True
            or any(
                isinstance(item, dict)
                and item.get("tool") == expected_tool
                and item.get("status") == "failed"
                for item in (turn.get("tool_trace") or [])
            )
        ]
        if tier != "openclaw_only" and incidents:
            evidence = list(dict.fromkeys(
                str(turn.get("turn_id") or "").strip()[:64]
                for turn in incidents if str(turn.get("turn_id") or "").strip()
            ))
            proposals.append(registry.propose_safety_downgrade(
                capability_id, evidence,
            ))
            continue
        if incidents:
            continue
        if registry.candidate(capability_id) is not None:
            continue
        if (
            active["permissions"]["risk"] != "read_only"
            or active["taxonomy"]["execution_kind"] != "deterministic_query"
        ):
            continue
        verified_by_id = {
            str(turn.get("turn_id") or "").strip()[:64]: turn for turn in bound
            if turn.get("success") is True
            and turn.get("risk_class") == "read_only"
            and _verified_tool_turn(turn, expected_tool)
            and str(turn.get("turn_id") or "").strip()
        }
        verified = list(verified_by_id.values())
        target = None
        threshold = minimum_days = minimum_expressions = 0
        if tier == "openclaw_only":
            target, threshold, minimum_days, minimum_expressions = "4b_eligible", 5, 2, 3
        elif tier == "4b_eligible":
            target, threshold, minimum_days, minimum_expressions = "08b_eligible", 10, 3, 5
        days = {_evidence_day(turn) for turn in verified} - {""}
        expressions = {_normalized_request(turn) for turn in verified} - {""}
        if (
            target is not None
            and len(verified) >= threshold
            and len(days) >= minimum_days
            and len(expressions) >= minimum_expressions
        ):
            evidence = list(verified_by_id)
            proposals.append(registry.propose_tier_change(
                capability_id, target, evidence,
            ))
    return proposals


_DISCOVERY_FORBIDDEN_TOOLS = {"exec", "shell", "web", "browser", "external_home.device_action"}
_DISCOVERY_CACHE_VERSION = 3


def discover_quick_tool_drafts(
    turns: list[dict[str, Any]], registry: CapabilityRegistry,
) -> list[dict[str, Any]]:
    """Aggregate trusted semantic keys into review-only drafts, never Registry entries."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for turn in turns:
        key = str(turn.get("discovery_key") or "").strip()
        trace = turn.get("tool_trace") or []
        if (
            not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", key)
            or registry.active(key) is not None
            or turn.get("execution_class") != "openclaw"
            or turn.get("success") is not True
            or turn.get("risk_class") != "read_only"
            or not isinstance(trace, list) or not trace
            or any(
                not isinstance(item, dict)
                or item.get("status") != "completed"
                or str(item.get("tool") or "") in _DISCOVERY_FORBIDDEN_TOOLS
                for item in trace
            )
        ):
            continue
        groups.setdefault(key, []).append(turn)

    drafts: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        unique = {
            str(row.get("turn_id") or "").strip()[:64]: row for row in rows
            if str(row.get("turn_id") or "").strip()
        }
        values = list(unique.values())
        days = {_evidence_day(row) for row in values} - {""}
        examples = list(dict.fromkeys(
            value for row in values if (value := _normalized_request(row))
        ))
        signatures = {
            tuple(f"{item.get('provider')}:{item.get('tool')}" for item in row["tool_trace"])
            for row in values
        }
        if len(values) < 3 or len(days) < 2 or len(examples) < 2 or len(signatures) != 1:
            continue
        drafts.append({
            "schema_version": 1,
            "key": key,
            "status": "needs_recipe_review",
            "initial_tier": "openclaw_only",
            "recipe_eligible": False,
            "observed_tool_signature": list(next(iter(signatures))),
            "examples": examples[:20],
            "evidence_ids": list(unique)[:100],
            "days": sorted(days),
            "confidence": 1.0,
        })
    return drafts


def propose_observed_capabilities(
    turns_path: Path, registry: CapabilityRegistry,
) -> list[dict[str, Any]]:
    proposals: dict[str, dict[str, Any]] = {}
    for turn in _read_json_lines(Path(turns_path)):
        if turn.get("success") is not True or turn.get("risk_class") != "read_only":
            continue
        level2 = turn.get("level2")
        if not isinstance(level2, dict) or level2.get("decision") != "quick_tool":
            continue
        capability_id = str(level2.get("quick_tool_id") or "")
        active = registry.active(capability_id) if capability_id else None
        trace = turn.get("tool_trace")
        if (
            active is None
            or registry.candidate(capability_id) is not None
            or not isinstance(trace, list) or not trace
            or any(
                not isinstance(item, dict)
                or item.get("status") != "completed"
                or not str(item.get("provider") or "")
                or not str(item.get("tool") or "")
                for item in trace
            )
            or not _completed_tool(turn, active["recipe"]["tool"])
        ):
            continue
        request = str(turn.get("request") or "").strip()[:200]
        evidence_id = str(turn.get("turn_id") or "").strip()[:64]
        if not request or not evidence_id:
            continue
        candidate = json.loads(json.dumps(active))
        candidate["version"] = active["version"] + 1
        candidate["status"] = "proposed"
        for path in (candidate["routing"]["examples"], candidate["selector"]["positive_examples"]):
            if request not in path:
                path.append(request)
        try:
            proposals[capability_id] = registry.propose(candidate, [evidence_id])
        except ValueError:
            continue
    return list(proposals.values())


def run_daily_v2(
    feedback_root: Path,
    registry: CapabilityRegistry,
    evaluator: Callable[[list[dict[str, Any]]], dict[str, float]],
    discovery_classifier: Callable[[str], str] | None = None,
    discovery_date: str | None = None,
) -> dict[str, Any]:
    root = Path(feedback_root)
    archived = archive_legacy_examples(root)
    evolution = Level1Evolution(root)
    candidates = collect_level1_candidates(root / "evolution-feedback-v2.jsonl")
    active_by_request = {
        str(item.get("request") or ""): item
        for item in evolution._read(evolution.active)
        if isinstance(item, dict)
    }
    candidates = [
        item for item in candidates
        if (
            item["decision"], item["quick_tool_id"], item["handoff"]
        ) != (
            str(active_by_request.get(item["request"], {}).get("decision") or ""),
            str(active_by_request.get(item["request"], {}).get("quick_tool_id") or ""),
            str(active_by_request.get(item["request"], {}).get("handoff") or ""),
        )
    ]
    if candidates:
        evolution.write_candidates(candidates)
    level1 = evolution.promote_if_safe(evaluator)
    turns = _read_json_lines(root / "turns.jsonl")
    tier_proposals = propose_capability_tier_changes(turns, registry)
    discoveries = discover_quick_tool_drafts(turns, registry)
    pool = semantic_pool(turns)
    discovery_path = root / "quick-tool-discoveries.json"
    existing_drafts = _validated_drafts(_read_json(discovery_path))
    bindings_path = root / "quick-tool-turn-bindings.json"
    turn_bindings = _validated_bindings(_read_json(bindings_path))
    tombstones_path = root / "quick-tool-discovery-tombstones.json"
    tombstones = _validated_tombstones(_read_json(tombstones_path))
    tombstones.update(
        item["id"] for item in registry.list_active()
        if re.fullmatch(r"[a-z][a-z0-9_]{2,63}", str(item.get("id") or ""))
    )
    conflicts_path = root / "quick-tool-discovery-conflicts.json"
    cache_path = root / "quick-tool-discovery-cache.json"
    cache = _validated_discovery_cache(_read_json(cache_path))
    gate_path = root / "quick-tool-discovery-cloud-gate.json"
    gate = _read_json(gate_path)
    today = discovery_date or datetime.now().astimezone().date().isoformat()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", today):
        raise ValueError("invalid discovery date")
    pool_hash = discovery_input_hash(
        [{"discovery_cache_version": _DISCOVERY_CACHE_VERSION}]
        + pool
        + [{"existing_draft": item} for item in existing_drafts]
        + [{"turn_binding": [turn_id, key]} for turn_id, key in sorted(turn_bindings.items())]
        + [{"tombstone": key} for key in sorted(tombstones)]
        + [{
            "capability_id": item["id"], "capability_version": item["version"],
        } for item in registry.list_active()]
    )
    discovery_status = "not_enough_evidence"
    discovery_conflicts: list[dict[str, Any]] | None = None
    if cache is not None and cache["input_hash"] == pool_hash:
        semantic_discoveries = cache.get("drafts") or []
        discovery_status = "cached"
    elif isinstance(gate, dict) and gate.get("last_attempt_date") == today:
        semantic_discoveries = existing_drafts
        discovery_status = "daily_limit"
    elif discovery_classifier is not None and len(pool) >= 3:
        _write_private(gate_path, {"last_attempt_date": today})
        try:
            classified_drafts, discovery_conflicts = discover_openclaw_patterns(
                turns, registry, classifier=discovery_classifier,
                existing_drafts=existing_drafts, turn_bindings=turn_bindings,
                tombstones=tombstones, return_conflicts=True,
            )
            semantic_by_key = {item["key"]: item for item in existing_drafts}
            semantic_by_key.update({item["key"]: item for item in classified_drafts})
            semantic_discoveries = list(semantic_by_key.values())
        except Exception:
            semantic_discoveries = existing_drafts
            discovery_status = "classifier_failed"
        else:
            discovery_status = "classified"
    else:
        semantic_discoveries = existing_drafts
    deterministic_source = discoveries if discovery_classifier is None else []
    safe_deterministic: list[dict[str, Any]] = []
    deterministic_conflicts: list[dict[str, Any]] = []
    existing_keys_by_signature: dict[tuple[str, ...], list[str]] = {}
    for existing in existing_drafts:
        existing_keys_by_signature.setdefault(
            tuple(existing["observed_tool_signature"]), []
        ).append(existing["key"])
    deterministic_keys_by_signature: dict[tuple[str, ...], list[str]] = {}
    for item in deterministic_source:
        deterministic_keys_by_signature.setdefault(
            tuple(item["observed_tool_signature"]), []
        ).append(item["key"])
    existing_draft_keys = {item["key"] for item in existing_drafts}
    deterministic_new_keys = {
        item["key"] for item in deterministic_source if item["key"] not in existing_draft_keys
    }
    for item in deterministic_source:
        conflicting = sorted({
            turn_bindings[turn_id] for turn_id in item["evidence_ids"]
            if turn_id in turn_bindings and turn_bindings[turn_id] != item["key"]
        })
        signature = tuple(item["observed_tool_signature"])
        existing_signature_keys = sorted(
            key for key in existing_keys_by_signature.get(signature, [])
            if key != item["key"]
        )
        same_batch_keys = sorted(set(deterministic_keys_by_signature.get(signature, [])))
        catalog_blocks_new = item["key"] not in existing_draft_keys and bool(existing_drafts)
        multiple_new_keys = item["key"] not in existing_draft_keys and len(deterministic_new_keys) > 1
        if (
            item["key"] in tombstones or conflicting or existing_signature_keys
            or len(same_batch_keys) > 1 or catalog_blocks_new or multiple_new_keys
        ):
            if item["key"] in tombstones:
                reason = "tombstoned_key"
                possible_keys = []
            elif conflicting:
                reason = "turn_binding_conflict"
                possible_keys = conflicting
            elif existing_signature_keys:
                reason = "signature_matches_existing_draft"
                possible_keys = existing_signature_keys
            elif len(same_batch_keys) > 1:
                reason = "same_signature_multiple_new_keys"
                possible_keys = same_batch_keys
            elif catalog_blocks_new:
                reason = "existing_catalog_requires_review"
                possible_keys = sorted(existing_draft_keys)
            else:
                reason = "multiple_new_keys_require_review"
                possible_keys = sorted(deterministic_new_keys)
            deterministic_conflicts.append({
                "reason": reason, "proposed_key": item["key"], "possible_keys": possible_keys,
                "turn_ids": list(item["evidence_ids"]),
            })
            continue
        safe_deterministic.append(item)
    if deterministic_conflicts and discovery_conflicts is None:
        discovery_conflicts = []
    if discovery_conflicts is not None:
        discovery_conflicts.extend(deterministic_conflicts)
    by_key = {item["key"]: item for item in semantic_discoveries}
    for item in safe_deterministic:
        previous = by_key.get(item["key"])
        if previous is None:
            by_key[item["key"]] = item
            continue
        if previous["observed_tool_signature"] != item["observed_tool_signature"]:
            continue
        merged = dict(previous)
        merged["examples"] = list(dict.fromkeys(previous["examples"] + item["examples"]))[:20]
        merged["evidence_ids"] = list(dict.fromkeys(
            previous["evidence_ids"] + item["evidence_ids"]
        ))[:100]
        merged["days"] = sorted(set(previous["days"] + item["days"]))
        merged["confidence"] = max(previous["confidence"], item["confidence"])
        by_key[item["key"]] = merged
    discoveries = [
        item for item in by_key.values()
        if registry.active(str(item.get("key") or "")) is None
    ]
    discoveries = validate_discovery_drafts(discoveries)
    existing_keys = {item["key"] for item in existing_drafts}
    ordered = sorted(discoveries, key=lambda item: (item["key"] not in existing_keys, item["key"]))
    accepted: list[dict[str, Any]] = []
    owners = dict(turn_bindings)
    overlap_conflicts: list[dict[str, Any]] = []
    for item in ordered:
        conflicting_keys = sorted({
            owners[turn_id] for turn_id in item["evidence_ids"]
            if turn_id in owners and owners[turn_id] != item["key"]
        })
        if conflicting_keys:
            overlap_conflicts.append({
                "reason": "turn_binding_conflict", "proposed_key": item["key"],
                "possible_keys": conflicting_keys, "turn_ids": list(item["evidence_ids"]),
            })
            continue
        accepted.append(item)
        for turn_id in item["evidence_ids"]:
            owners[turn_id] = item["key"]
    discoveries = accepted
    turn_bindings = owners
    if discovery_conflicts is not None:
        discovery_conflicts.extend(overlap_conflicts)
    _write_private(discovery_path, discoveries)
    _write_private(bindings_path, turn_bindings)
    _write_private(tombstones_path, sorted(tombstones))
    if discovery_conflicts is not None:
        _write_private(conflicts_path, discovery_conflicts)
    if discovery_status == "classified":
        final_hash = discovery_input_hash(
            [{"discovery_cache_version": _DISCOVERY_CACHE_VERSION}]
            + pool
            + [{"existing_draft": item} for item in discoveries]
            + [{"turn_binding": [turn_id, key]} for turn_id, key in sorted(turn_bindings.items())]
            + [{"tombstone": key} for key in sorted(tombstones)]
            + [{
                "capability_id": item["id"], "capability_version": item["version"],
            } for item in registry.list_active()]
        )
        _write_private(cache_path, {
            "input_hash": final_hash, "drafts": discoveries,
        })
    proposals = tier_proposals + propose_observed_capabilities(
        root / "turns.jsonl", registry,
    )
    result = {
        "status": "ok", "level1": level1,
        "capability_candidate_count": len(proposals),
        "quick_tool_discovery_count": len(discoveries),
        "quick_tool_discovery_status": discovery_status,
        "legacy_archived": archived,
    }
    _write_private(root / "last-evolution-v2.json", result)
    return result


def run_production_daily(
    feedback_root: Path, registry: CapabilityRegistry, *,
    evaluator_factory: Callable[[CapabilityRegistry], Callable[[list[dict[str, Any]]], dict[str, float]]] | None = None,
    discovery_classifier: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    factory = evaluator_factory or (
        lambda current: lambda examples: ollama_level1_evaluator(
            examples, registry=current,
        )
    )
    if discovery_classifier is None:
        from review_job import direct_provider_reviewer
        discovery_classifier = direct_provider_reviewer
    with evolution_lock(feedback_root):
        return run_daily_v2(
            feedback_root, registry, factory(registry),
            discovery_classifier=discovery_classifier,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--feedback", type=Path,
        default=Path(os.getenv("JARVIS_FEEDBACK_STATE", "./state/route-feedback")),
    )
    parser.add_argument(
        "--capabilities", type=Path,
        default=Path(os.getenv("JARVIS_CAPABILITY_STATE", "./state/capabilities")),
    )
    args = parser.parse_args()
    result = run_production_daily(
        args.feedback, CapabilityRegistry(args.capabilities),
    )
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
