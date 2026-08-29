"""Versioned 0.8B prompt-example promotion and rollback gates."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class PromptEvolution:
    def __init__(self, root: Path, *, max_p95_seconds: float = 1.5) -> None:
        self.root = Path(root)
        self.active = self.root / "router-examples.json"
        self.candidate = self.root / "router-candidate-examples.json"
        self.previous = self.root / "router-previous-examples.json"
        self.report = self.root / "last-prompt-promotion.json"
        self.max_p95_seconds = max_p95_seconds

    @staticmethod
    def _read(path: Path) -> list[dict[str, str]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return value if isinstance(value, list) else []

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    def promote_if_safe(
        self, evaluator: Callable[[list[dict[str, str]]], dict[str, float]]
    ) -> dict[str, Any]:
        active = self._read(self.active)
        candidate = self._read(self.candidate)
        if not candidate:
            result: dict[str, Any] = {"status": "skipped", "reason": "no_candidates"}
            self._write(self.report, result)
            return result
        candidate_requests = {str(item.get("request") or "") for item in candidate}
        merged = [
            item for item in active
            if str(item.get("request") or "") not in candidate_requests
        ] + candidate
        merged = merged[-20:]
        metrics = evaluator(merged)
        passed = (
            float(metrics.get("valid_json_rate", 0)) == 1.0
            and float(metrics.get("baseline_accuracy", 0)) == 1.0
            and float(metrics.get("candidate_accuracy", 0)) == 1.0
            and float(metrics.get("p95_seconds", 999)) <= self.max_p95_seconds
        )
        result = {
            "status": "promoted" if passed else "rejected",
            "time": datetime.now(timezone.utc).isoformat(),
            "candidate_count": len(candidate),
            "metrics": metrics,
        }
        if passed:
            self._write(self.previous, active)
            self._write(self.active, merged)
            self._write(self.candidate, [])
        self._write(self.report, result)
        return result

    def rollback(self) -> dict[str, Any]:
        previous = self._read(self.previous)
        if not previous:
            result: dict[str, Any] = {"status": "skipped", "reason": "no_previous_version"}
        else:
            current = self._read(self.active)
            self._write(self.active, previous)
            self._write(self.previous, current)
            result = {"status": "rolled_back", "time": datetime.now(timezone.utc).isoformat()}
        self._write(self.report, result)
        return result
