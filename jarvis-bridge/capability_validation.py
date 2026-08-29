"""Server-side replay, shadow, and security validation for capability candidates."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

from capability_registry import CapabilityRegistry, CapabilityValidationError


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_private_once(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    if path.exists():
        if path.read_text(encoding="utf-8") != payload:
            raise CapabilityValidationError("validation report hash collision")
        return
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(payload, encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    path.chmod(0o600)


class CapabilityValidator:
    def __init__(
        self,
        root: Path,
        registry: CapabilityRegistry,
        recipe_runtime: Any,
        *,
        replay_runner: Callable[[dict[str, Any]], dict[str, Any]],
        dangerous_negative_runner: Callable[
            [dict[str, Any], str], dict[str, Any]
        ] | None = None,
    ) -> None:
        self.root = Path(root)
        self.registry = registry
        self.recipe_runtime = recipe_runtime
        self.replay_runner = replay_runner
        self.dangerous_negative_runner = dangerous_negative_runner

    def report_path(self, report_hash: str) -> Path:
        if len(report_hash) != 64 or any(ch not in "0123456789abcdef" for ch in report_hash):
            raise CapabilityValidationError("invalid validation report hash")
        return self.root / f"{report_hash}.json"

    def report(self, report_hash: str) -> dict[str, Any] | None:
        try:
            value = json.loads(self.report_path(report_hash).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def validate_candidate(self, capability_id: str) -> dict[str, Any]:
        active = self.registry.active(capability_id)
        proposal = self.registry.candidate(capability_id)
        if active is None or not isinstance(proposal, dict):
            raise CapabilityValidationError("active capability or candidate not found")
        candidate = self.registry.validate(proposal.get("capability"))
        if proposal.get("from_version") != active["version"]:
            raise CapabilityValidationError("candidate is stale")
        change_kind = str(proposal.get("change_kind") or "content")
        from_tier = str(
            proposal.get("from_tier") or active["selector"]["tier"]
        )
        target_tier = str(
            proposal.get("target_tier") or candidate["selector"]["tier"]
        )
        if change_kind == "selector_tier" and target_tier == "openclaw_only":
            self.registry.validate(candidate)
            gates = {"security": {
                "passed": True,
                "risk": candidate["permissions"]["risk"],
                "tool": candidate["recipe"]["tool"],
            }}
            body = {
                "schema_version": 2,
                "change_kind": "selector_tier",
                "from_tier": from_tier,
                "target_tier": target_tier,
                "capability_id": capability_id,
                "active_version": active["version"],
                "active_hash": canonical_hash(active),
                "candidate_version": candidate["version"],
                "candidate_hash": canonical_hash(candidate),
                "gates": gates,
                "passed": True,
            }
            report_hash = canonical_hash(body)
            report = {**body, "report_hash": report_hash}
            _write_private_once(self.report_path(report_hash), report)
            return report

        replay = dict(self.replay_runner(candidate))
        replay_passed = replay.get("passed") is True

        active_result = self.recipe_runtime.execute(active)
        candidate_result = self.recipe_runtime.execute(candidate)
        shadow = {
            "passed": active_result == candidate_result,
            "active_result_hash": canonical_hash(active_result),
            "candidate_result_hash": canonical_hash(candidate_result),
        }

        # validate() is the authoritative fail-closed schema, DSL, permission,
        # ownership, and read-only policy check.
        self.registry.validate(candidate)
        security = {
            "passed": True,
            "risk": candidate["permissions"]["risk"],
            "tool": candidate["recipe"]["tool"],
        }
        if (
            change_kind == "selector_tier"
            and target_tier in {"4b_eligible", "08b_eligible"}
        ):
            layer = "4b" if target_tier == "4b_eligible" else "08b"
            dangerous = (
                dict(self.dangerous_negative_runner(candidate, layer))
                if self.dangerous_negative_runner is not None
                else {
                    "passed": False,
                    "sample_count": 0,
                    "reason": "runner_not_configured",
                }
            )
            gates = {
                f"replay_{layer}": replay,
                f"dangerous_negatives_{layer}": dangerous,
                "shadow": shadow,
                "security": security,
            }
            body = {
                "schema_version": 2,
                "change_kind": "selector_tier",
                "from_tier": from_tier,
                "target_tier": target_tier,
                "capability_id": capability_id,
                "active_version": active["version"],
                "active_hash": canonical_hash(active),
                "candidate_version": candidate["version"],
                "candidate_hash": canonical_hash(candidate),
                "gates": gates,
                "passed": (
                    replay_passed
                    and dangerous.get("passed") is True
                    and shadow["passed"]
                    and security["passed"]
                ),
            }
        else:
            gates = {"replay": replay, "shadow": shadow, "security": security}
            body = {
                "schema_version": 1,
                "capability_id": capability_id,
                "active_version": active["version"],
                "active_hash": canonical_hash(active),
                "candidate_version": candidate["version"],
                "candidate_hash": canonical_hash(candidate),
                "gates": gates,
                "passed": (
                    replay_passed and shadow["passed"] and security["passed"]
                ),
            }
        report_hash = canonical_hash(body)
        report = {**body, "report_hash": report_hash}
        _write_private_once(self.report_path(report_hash), report)
        return report
