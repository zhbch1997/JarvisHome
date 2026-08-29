"""User-correction ledger and strict OpenClaw route-review contract."""
from __future__ import annotations

import json
import os
import re

import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_ALLOWED_ROUTES = {"task", "camera", "home", "web_query", "local_chat"}
_ALLOWED_INTENTS = {
    "create", "list", "update", "delete", "query", "action", "chat",
    "camera_recent", "camera_live",
}
_ALLOWED_EXECUTORS = {"external_home", "local_4b", "local_9b", "openclaw"}
_MUTATING_INTENTS = {"create", "update", "delete", "action"}



def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)



@dataclass(frozen=True)
class RouteReview:
    route: str
    intent: str
    executor: str
    confidence: float
    safe_to_retry: bool
    reason: str
    capability: str = "none"

    @property
    def can_auto_retry(self) -> bool:
        return (
            self.safe_to_retry
            and self.intent not in _MUTATING_INTENTS
            and self.confidence >= 0.8
        )


def parse_review(raw: str) -> RouteReview:
    try:
        value = json.loads(str(raw or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("route review is not valid JSON") from exc
    required = {"route", "intent", "executor", "confidence", "safe_to_retry", "reason"}
    if not isinstance(value, dict) or set(value) not in {frozenset(required), frozenset(required | {"capability"})}:
        raise ValueError("route review schema mismatch")
    route = str(value["route"])
    intent = str(value["intent"])
    executor = str(value["executor"])
    capability = str(value.get("capability") or "none")
    confidence = float(value["confidence"])
    reason = str(value["reason"]).strip()[:300]
    if route not in _ALLOWED_ROUTES or intent not in _ALLOWED_INTENTS:
        raise ValueError("route review contains unsupported route or intent")
    if executor not in _ALLOWED_EXECUTORS:
        raise ValueError("route review contains unsupported executor")
    if not 0 <= confidence <= 1 or not isinstance(value["safe_to_retry"], bool) or not reason:
        raise ValueError("route review contains invalid fields")
    if route == "camera" and executor != "external_home":
        raise ValueError("camera review must use external_home")
    if route == "local_chat" and executor != "local_4b":
        raise ValueError("local chat review must use local_4b")
    if route == "web_query" and (intent != "query" or executor != "openclaw"):
        raise ValueError("web query review must use query/openclaw")
    if capability not in {"none", "camera_inventory"}:
        raise ValueError("route review contains unsupported capability")
    if capability == "camera_inventory" and (route, intent, executor) != ("home", "query", "external_home"):
        raise ValueError("camera inventory capability must use home/query/external_home")
    return RouteReview(route, intent, executor, confidence, value["safe_to_retry"], reason, capability)


class FeedbackLedger:
    def __init__(self, root: Path, now=None) -> None:
        self.root = Path(root)
        self._clock = now or (lambda: datetime.now(timezone.utc).astimezone())
        self.last_turn_path = self.root / "last-turn.json"
        self.turns_path = self.root / "turns.jsonl"
        self.feedback_path = self.root / "feedback.jsonl"
        self.active_session_path = self.root / "active-session.json"
        self.speaker_sessions_path = self.root / "speaker-sessions.json"

    def _resolve_speaker_session(self, channel_id: str) -> str:
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        sessions: dict[str, dict[str, str]] = {}
        try:
            loaded = json.loads(self.speaker_sessions_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                sessions = loaded
        except (OSError, json.JSONDecodeError):
            pass
        current = sessions.get(channel_id) if isinstance(sessions.get(channel_id), dict) else {}
        session_id = ""
        try:
            updated = datetime.fromisoformat(str(current.get("updated_at") or ""))
            same_day = str(current.get("local_date") or "") == now.date().isoformat()
            if same_day and now - updated <= timedelta(minutes=30):
                session_id = str(current.get("session_id") or "")
        except (ValueError, TypeError):
            pass
        if not session_id:
            session_id = uuid.uuid4().hex
        timestamp = now.isoformat()
        sessions[channel_id] = {
            "session_id": session_id,
            "updated_at": timestamp,
            "local_date": now.date().isoformat(),
        }
        _private_write(self.speaker_sessions_path, json.dumps(sessions, ensure_ascii=False, indent=2))
        _private_write(self.active_session_path, json.dumps({
            "session_id": session_id, "updated_at": timestamp,
        }, ensure_ascii=False))
        return session_id

    def resolve_session(self, requested: str | None = None) -> str:
        explicit = str(requested or "").strip()
        if explicit:
            if len(explicit) > 128 or not all(ch.isalnum() or ch in "-_" for ch in explicit):
                raise ValueError("invalid session_id")
            if explicit.startswith("speaker-"):
                return self._resolve_speaker_session(explicit)
            # Explicit web sessions never replace the speaker's active-session pointer.
            return explicit
        else:
            session_id = ""
            try:
                active = json.loads(self.active_session_path.read_text(encoding="utf-8"))
                updated = datetime.fromisoformat(str(active.get("updated_at") or ""))
                if datetime.now(timezone.utc) - updated <= timedelta(minutes=30):
                    session_id = str(active.get("session_id") or "")
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass
            if not session_id:
                session_id = uuid.uuid4().hex
        _private_write(self.active_session_path, json.dumps({
            "session_id": session_id, "updated_at": _now(),
        }, ensure_ascii=False))
        return session_id

    def save_turn(self, value: dict[str, Any]) -> dict[str, Any]:
        required = {"request", "route", "intent", "executor", "answer", "success"}
        optional = {
            "session_id", "producer", "capability", "capability_version",
            "escalation_reason", "tool_trace",
            "arbitration_id", "level1", "level2", "decision_tier",
            "execution_class", "tool_class", "selector_tier",
            "risk_class", "execution_elapsed_ms",
        }
        if not required.issubset(value) or set(value) - required - optional:
            raise ValueError("turn schema mismatch")
        session_id = self.resolve_session(value.get("session_id"))
        tool_trace = []
        for item in value.get("tool_trace") or []:
            if not isinstance(item, dict):
                continue
            provider = str(item.get("provider") or "")
            tool = str(item.get("tool") or "")[:64]
            status = str(item.get("status") or "")
            elapsed_ms = item.get("elapsed_ms")
            if (
                provider in {"openclaw", "external_home", "local_9b_agent"}
                and status in {"started", "completed", "failed"}
                and tool
                and all(ch.isalnum() or ch in "._-" for ch in tool)
            ):
                safe_item = {"provider": provider, "tool": tool, "status": status}
                if (
                    isinstance(elapsed_ms, int) and not isinstance(elapsed_ms, bool)
                    and 0 <= elapsed_ms <= 600_000
                ):
                    safe_item["elapsed_ms"] = elapsed_ms
                tool_trace.append(safe_item)
            if len(tool_trace) >= 20:
                break
        arbitration_id = str(value.get("arbitration_id") or "")
        if arbitration_id and not re.fullmatch(r"arb-[a-zA-Z0-9_-]{3,64}", arbitration_id):
            raise ValueError("invalid arbitration id")
        decision_tier = str(value.get("decision_tier") or "legacy")
        execution_class = str(value.get("execution_class") or "legacy")
        tool_class = str(value.get("tool_class") or "none")
        selector_tier = str(value.get("selector_tier") or "openclaw_only")
        risk_class = str(value.get("risk_class") or "unrecorded")
        if decision_tier not in {"legacy", "0.8b", "4b", "openclaw"}:
            raise ValueError("invalid decision tier")
        if execution_class not in {"legacy", "quick_tool", "local_chat", "openclaw"}:
            raise ValueError("invalid execution class")
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}|none", tool_class):
            raise ValueError("invalid tool class")
        if selector_tier not in {"08b_eligible", "4b_eligible", "openclaw_only"}:
            raise ValueError("invalid selector tier")
        if risk_class not in {
            "unrecorded", "read_only", "reversible_write", "device_action", "high_risk",
        }:
            raise ValueError("invalid risk class")

        level1 = None
        if value.get("level1") is not None:
            source = value["level1"]
            if not isinstance(source, dict):
                raise ValueError("invalid level1 metadata")
            level1 = {
                "decision": str(source.get("decision") or ""),
                "quick_tool_id": str(source.get("quick_tool_id") or "none")[:64],
                "handoff": str(source.get("handoff") or ""),
                "confidence": float(source.get("confidence") or 0),
            }
            elapsed_ms = source.get("elapsed_ms")
            if (
                isinstance(elapsed_ms, int) and not isinstance(elapsed_ms, bool)
                and 0 <= elapsed_ms <= 600_000
            ):
                level1["elapsed_ms"] = elapsed_ms
            if (
                level1["decision"] not in {"chat", "quick_tool", "handoff"}
                or level1["handoff"] not in {"none", "lookup", "analyze", "action", "general"}
                or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}|none", level1["quick_tool_id"])
                or not 0 <= level1["confidence"] <= 1
            ):
                raise ValueError("invalid level1 metadata")

        level2 = None
        if value.get("level2") is not None:
            source = value["level2"]
            if not isinstance(source, dict):
                raise ValueError("invalid level2 metadata")
            level2 = {
                "decision": str(source.get("decision") or ""),
                "quick_tool_id": str(source.get("quick_tool_id") or "none")[:64],
                "relation": str(source.get("relation") or ""),
                "confidence": float(source.get("confidence") or 0),
            }
            elapsed_ms = source.get("elapsed_ms")
            if (
                isinstance(elapsed_ms, int) and not isinstance(elapsed_ms, bool)
                and 0 <= elapsed_ms <= 600_000
            ):
                level2["elapsed_ms"] = elapsed_ms
            if (
                level2["decision"] not in {"quick_tool", "openclaw"}
                or level2["relation"] not in {"consistent", "overridden", "escalated"}
                or not re.fullmatch(r"[a-z][a-z0-9_]{2,63}|none", level2["quick_tool_id"])
                or not 0 <= level2["confidence"] <= 1
            ):
                raise ValueError("invalid level2 metadata")
        turn = {
            "turn_id": uuid.uuid4().hex,
            "session_id": session_id,
            "time": _now(),
            "request": str(value["request"])[:500],
            "route": str(value["route"]),
            "intent": str(value["intent"]),
            "executor": str(value["executor"]),
            "producer": str(value.get("producer") or value["executor"]),
            "capability": str(value.get("capability") or "none"),
            "capability_version": int(value.get("capability_version") or 0),
            "escalation_reason": str(value.get("escalation_reason") or "")[:64],
            "tool_trace": tool_trace,
            "arbitration_id": arbitration_id,
            "level1": level1,
            "level2": level2,
            "decision_tier": decision_tier,
            "execution_class": execution_class,
            "tool_class": tool_class,
            "selector_tier": selector_tier,
            "risk_class": risk_class,
            "answer": str(value["answer"])[:1000],
            "success": bool(value["success"]),
        }
        execution_elapsed_ms = value.get("execution_elapsed_ms")
        if (
            isinstance(execution_elapsed_ms, int)
            and not isinstance(execution_elapsed_ms, bool)
            and 0 <= execution_elapsed_ms <= 600_000
        ):
            turn["execution_elapsed_ms"] = execution_elapsed_ms
        _private_write(self.last_turn_path, json.dumps(turn, ensure_ascii=False, indent=2))
        self.root.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.turns_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, (json.dumps(turn, ensure_ascii=False) + "\n").encode())
        finally:
            os.close(descriptor)
        os.chmod(self.turns_path, 0o600)
        return turn

    def last_turn(self) -> dict[str, Any] | None:
        try:
            value = json.loads(self.last_turn_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) and value.get("turn_id") else None

    def get_turn(self, turn_id: str) -> dict[str, Any] | None:
        wanted = str(turn_id or "").strip()
        if not wanted:
            return None
        try:
            lines = self.turns_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        for line in reversed(lines):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("turn_id") == wanted:
                return value
        return None

    def list_turns(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 100))
        try:
            lines = self.turns_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        turns: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("turn_id"):
                required = {"time", "request", "route", "intent", "executor", "answer", "success"}
                if required.issubset(value):
                    item = dict(value)
                    # Old turns did not carry a real conversation boundary.
                    # Never guess grouping from timestamps: one legacy turn is one session.
                    item.setdefault("session_id", "legacy-" + str(item["turn_id"]))
                    turns.append(item)
        return turns[-bounded:]

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for turn in self.list_turns(100):
            session_id = str(turn["session_id"])
            current = grouped.setdefault(session_id, {
                "session_id": session_id, "title": str(turn["request"])[:80],
                "created_at": turn["time"], "updated_at": turn["time"],
                "turn_count": 0, "last_answer": "",
            })
            current["updated_at"] = turn["time"]
            current["turn_count"] += 1
            current["last_answer"] = str(turn["answer"])[:120]
        return sorted(grouped.values(), key=lambda item: item["updated_at"], reverse=True)[:max(1, min(limit, 100))]

    def session_turns(self, session_id: str) -> list[dict[str, Any]]:
        wanted = str(session_id or "").strip()
        return [turn for turn in self.list_turns(100) if turn.get("session_id") == wanted]

    def prompt_examples(self, limit: int = 20) -> list[dict[str, str]]:
        return self._read_examples(self.root / "router-examples.json", limit)

    def candidate_examples(self, limit: int = 20) -> list[dict[str, str]]:
        return self._read_examples(self.root / "router-candidate-examples.json", limit)

    @staticmethod
    def _read_examples(path: Path, limit: int) -> list[dict[str, str]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(value, list):
            return []
        allowed = {"request", "route", "intent", "executor"}
        return [item for item in value if isinstance(item, dict) and set(item) == allowed][-limit:]

    def _save_candidate_example(self, turn: dict[str, Any], review: RouteReview) -> None:
        example = {
            "request": str(turn["request"])[:500],
            "route": review.route,
            "intent": review.intent,
            "executor": review.executor,
        }
        examples = [
            item for item in self.candidate_examples()
            if item.get("request") != example["request"]
        ]
        examples.append(example)
        _private_write(
            self.root / "router-candidate-examples.json",
            json.dumps(examples[-20:], ensure_ascii=False, indent=2),
        )

    def append_feedback(
        self, turn: dict[str, Any], review: RouteReview, feedback_text: str
    ) -> dict[str, Any]:
        row = {
            "feedback_id": uuid.uuid4().hex,
            "time": _now(),
            "original_turn_id": str(turn["turn_id"]),
            "request": str(turn["request"])[:500],
            "original_route": str(turn["route"]),
            "original_intent": str(turn["intent"]),
            "original_executor": str(turn["executor"]),
            "original_success": bool(turn["success"]),
            "feedback": str(feedback_text)[:200],
            **{f"review_{key}": value for key, value in asdict(review).items()},
        }
        self.feedback_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.feedback_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(descriptor, (json.dumps(row, ensure_ascii=False) + "\n").encode())
        finally:
            os.close(descriptor)
        os.chmod(self.feedback_path, 0o600)
        changed = any((
            str(turn.get("route")) != review.route,
            str(turn.get("intent")) != review.intent,
            actual_executor(turn) != review.executor,
        ))
        if changed:
            self._save_candidate_example(turn, review)
        return row


def actual_executor(turn: dict[str, Any]) -> str:
    """Return the observed producer, falling back for legacy turns."""
    return str(turn.get("producer") or turn.get("executor") or "")


def build_review_prompt(turn: dict[str, Any], feedback: str) -> str:
    payload = {
        "request": str(turn.get("request") or "")[:500],
        "route": str(turn.get("route") or ""),
        "intent": str(turn.get("intent") or ""),
        "planned_executor": str(turn.get("executor") or ""),
        "executor": actual_executor(turn),
        "answer": str(turn.get("answer") or "")[:1000],
        "success": bool(turn.get("success")),
        "user_feedback": str(feedback)[:200],
    }
    return (
        "你是Jarvis路线复审器。用户明确不满意上一轮，请复审整个路线。"
        "只输出一个JSON对象，不输出Markdown或解释。"
        "route仅可为task/camera/home/web_query/local_chat；"
        "intent仅可为create/list/update/delete/query/action/chat/camera_recent/camera_live；"
        "executor表示实际结果生产者，仅可为external_home/local_4b/openclaw；planned_executor只是Bridge计划执行方式，不据此判错。"
        "web_query用于外部实时信息或联网检索且必须使用query/openclaw。"
        "capability仅可为camera_inventory或none；询问家中摄像头数量、清单、有哪些摄像头时用camera_inventory，且必须home/query/external_home；其他用none。"
        "safe_to_retry只有纯查询或聊天才可为true；创建、修改、删除、设备控制必须false。"
        "输出字段必须恰好为route,intent,executor,capability,confidence,safe_to_retry,reason。\n"
        + json.dumps(payload, ensure_ascii=False)
    )
