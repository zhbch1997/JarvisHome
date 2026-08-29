"""Versioned, fail-closed capability bundles for the stable Jarvis Bridge kernel."""
from __future__ import annotations

import json
import hashlib
import fcntl
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any


_ALLOWED_ROUTES = {"home", "camera", "local_chat", "task", "web_query"}
_ALLOWED_OPERATIONS = {
    "query", "action", "chat", "create", "list", "update", "delete",
    "camera_recent", "camera_live",
}
_ALLOWED_EXECUTORS = {"bridge_recipe", "external_home_direct", "local_9b", "local_9b_agent", "local_4b_device", "local_4b_scene", "openclaw"}
_ALLOWED_PRODUCERS = {"external_home", "local_9b", "local_9b_agent", "local_4b", "openclaw"}
_ALLOWED_TOOLS = {
    "external_home.device_list", "external_home.turtle_recent", "external_home.hamster_recent", "external_home.device_action", "external_home.scene_trigger",
}
_ALLOWED_TRANSFORMS = {"filter_eq"}
_ALLOWED_TEMPLATES = {
    "camera_inventory_zh", "turtle_recent_activity_zh", "hamster_recent_activity_zh", "device_action_zh", "scene_action_zh",
}
_LEGACY_EXPECTED_KEYS = {"id", "version", "status", "routing", "execution", "recipe", "permissions", "delivery", "review"}
_EXPECTED_KEYS = _LEGACY_EXPECTED_KEYS | {"selector", "taxonomy"}
_ALLOWED_SELECTOR_TIERS = {"08b_eligible", "4b_eligible", "openclaw_only"}
_ALLOWED_EXECUTION_KINDS = {
    "deterministic_query", "direct_read", "composed_read", "openclaw_tool", "device_action", "scene_action",
}


class CapabilityValidationError(ValueError):
    pass


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_private(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    path.chmod(0o600)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


class CapabilityRegistry:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.active_root = self.root / "active"
        self.candidate_root = self.root / "candidates"
        self.history_root = self.root / "history"
        self.version_root = self.root / "versions"
        self.lock_root = self.root / "locks"

    @contextmanager
    def _locked(self, capability_id: str):
        lock_path = self._path(self.lock_root, capability_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.parent.chmod(0o700)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _path(self, root: Path, capability_id: str) -> Path:
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,63}", str(capability_id or "")):
            raise CapabilityValidationError("invalid capability id")
        return root / f"{capability_id}.json"

    def _snapshot_path(self, bundle: dict[str, Any]) -> Path:
        capability_id = str(bundle["id"])
        digest = _canonical_hash(bundle)
        return self.version_root / capability_id / f"v{bundle['version']}-{digest}.json"

    def _write_snapshot(self, bundle: dict[str, Any]) -> tuple[Path, str]:
        path = self._snapshot_path(bundle)
        digest = _canonical_hash(bundle)
        if path.exists():
            if _read_json(path) != bundle:
                raise CapabilityValidationError("immutable capability snapshot mismatch")
        else:
            _write_private(path, bundle)
        return path, digest

    def _pointer(self, bundle: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
        path, digest = self._write_snapshot(bundle)
        return {
            "schema_version": 1, "id": bundle["id"], "version": bundle["version"],
            "bundle_hash": digest, "snapshot": str(path.relative_to(self.root)),
            "history": history,
        }

    def _read_pointer(self, capability_id: str, value: dict[str, Any]) -> dict[str, Any]:
        required = {"schema_version", "id", "version", "bundle_hash", "snapshot", "history"}
        if set(value) != required or value.get("schema_version") != 1 or value.get("id") != capability_id:
            raise CapabilityValidationError("invalid active capability pointer")
        snapshot = self.root / str(value["snapshot"])
        try:
            snapshot.relative_to(self.version_root)
        except ValueError as exc:
            raise CapabilityValidationError("active pointer escapes version root") from exc
        bundle = _read_json(snapshot)
        if not isinstance(bundle, dict) or _canonical_hash(bundle) != value["bundle_hash"]:
            raise CapabilityValidationError("active capability snapshot missing or corrupt")
        validated = self.validate(bundle)
        if validated["version"] != value["version"] or not isinstance(value["history"], list):
            raise CapabilityValidationError("active pointer metadata mismatch")
        return validated

    def validate(self, bundle: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(bundle, dict) or frozenset(bundle) not in {
            frozenset(_LEGACY_EXPECTED_KEYS), frozenset(_EXPECTED_KEYS),
        }:
            raise CapabilityValidationError("capability bundle schema mismatch")
        bundle = json.loads(json.dumps(bundle))
        if not isinstance(bundle.get("routing"), dict):
            raise CapabilityValidationError("routing schema mismatch")
        if not isinstance(bundle.get("execution"), dict):
            raise CapabilityValidationError("execution schema mismatch")
        if "selector" not in bundle:
            bundle["selector"] = {
                "tier": "openclaw_only",
                "description": str(bundle["id"]).replace("_", " "),
                "positive_examples": list(bundle["routing"].get("examples") or []),
                "dangerous_negatives": [],
                "min_confidence": {"0.8b": 0.99, "4b": 0.95},
            }
        if "taxonomy" not in bundle:
            primary_value = bundle["execution"].get("primary")
            execution_kind = {
                "bridge_recipe": "deterministic_query",
                "external_home_direct": "direct_read",
                "openclaw": "openclaw_tool",
            }.get(primary_value, "composed_read") if isinstance(primary_value, str) else "composed_read"
            bundle["taxonomy"] = {
                "domain_tags": [str(bundle["routing"].get("route") or "general")],
                "execution_kind": execution_kind,
            }
        capability_id = str(bundle["id"])
        self._path(self.active_root, capability_id)
        if not isinstance(bundle["version"], int) or bundle["version"] < 1:
            raise CapabilityValidationError("invalid capability version")
        if bundle["status"] not in {"active", "proposed"}:
            raise CapabilityValidationError("invalid capability status")

        routing = bundle["routing"]
        if not isinstance(routing, dict) or set(routing) != {"route", "operation", "examples"}:
            raise CapabilityValidationError("routing schema mismatch")
        if routing["route"] not in _ALLOWED_ROUTES or routing["operation"] not in _ALLOWED_OPERATIONS:
            raise CapabilityValidationError("unsupported route or operation")
        if not isinstance(routing["examples"], list) or not routing["examples"] or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200 for item in routing["examples"]
        ):
            raise CapabilityValidationError("invalid routing examples")

        selector = bundle["selector"]
        if not isinstance(selector, dict) or set(selector) != {
            "tier", "description", "positive_examples", "dangerous_negatives", "min_confidence",
        }:
            raise CapabilityValidationError("selector schema mismatch")
        if selector["tier"] not in _ALLOWED_SELECTOR_TIERS:
            raise CapabilityValidationError("invalid selector tier")
        if not isinstance(selector["description"], str) or not 1 <= len(selector["description"].strip()) <= 200:
            raise CapabilityValidationError("invalid selector description")
        for key in ("positive_examples", "dangerous_negatives"):
            examples = selector[key]
            if not isinstance(examples, list) or any(
                not isinstance(item, str) or not item.strip() or len(item) > 200 for item in examples
            ):
                raise CapabilityValidationError("invalid selector examples")
        if not selector["positive_examples"]:
            raise CapabilityValidationError("selector needs positive examples")
        confidence = selector["min_confidence"]
        if not isinstance(confidence, dict) or set(confidence) != {"0.8b", "4b"} or any(
            isinstance(confidence[key], bool) or not isinstance(confidence[key], (int, float))
            or not 0 <= confidence[key] <= 1 for key in confidence
        ):
            raise CapabilityValidationError("invalid selector confidence")

        taxonomy = bundle["taxonomy"]
        if not isinstance(taxonomy, dict) or set(taxonomy) != {"domain_tags", "execution_kind"}:
            raise CapabilityValidationError("taxonomy schema mismatch")
        tags = taxonomy["domain_tags"]
        if not isinstance(tags, list) or not tags or len(tags) > 16 or len(set(tags)) != len(tags) or any(
            not isinstance(tag, str) or not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", tag) for tag in tags
        ):
            raise CapabilityValidationError("invalid domain tags")
        if taxonomy["execution_kind"] not in _ALLOWED_EXECUTION_KINDS:
            raise CapabilityValidationError("invalid execution kind")

        execution = bundle["execution"]
        if not isinstance(execution, dict) or set(execution) != {"primary", "producer", "fallback", "recipe"}:
            raise CapabilityValidationError("execution schema mismatch")
        primary = execution["primary"]
        producer = execution["producer"]
        fallback = execution["fallback"]
        if not isinstance(primary, str) or not isinstance(producer, str):
            raise CapabilityValidationError("executor and producer must each be one string")
        if primary not in _ALLOWED_EXECUTORS or producer not in _ALLOWED_PRODUCERS:
            raise CapabilityValidationError("unsupported executor or producer")
        if fallback is not None and fallback not in _ALLOWED_EXECUTORS:
            raise CapabilityValidationError("unsupported fallback")
        if primary == fallback:
            raise CapabilityValidationError("executor fallback cycle")
        expected_producer = {
            "bridge_recipe": "external_home", "external_home_direct": "external_home", "local_9b": "local_9b",
            "local_9b_agent": "local_9b_agent", "openclaw": "openclaw",
            "local_4b_device": "local_4b", "local_4b_scene": "local_4b",
        }[primary]
        if producer != expected_producer:
            raise CapabilityValidationError("executor and producer mismatch")
        allowed_execution_kinds = {
            "bridge_recipe": {"deterministic_query", "composed_read"},
            "external_home_direct": {"direct_read"},
            "openclaw": {"openclaw_tool"},
            "local_9b": {"composed_read"},
            "local_9b_agent": {"composed_read"},
            "local_4b_device": {"device_action"},
            "local_4b_scene": {"scene_action"},
        }[primary]
        if taxonomy["execution_kind"] not in allowed_execution_kinds:
            raise CapabilityValidationError("execution kind and executor mismatch")
        if not isinstance(execution["recipe"], str) or not execution["recipe"].startswith(capability_id + "_v"):
            raise CapabilityValidationError("recipe name must be versioned with capability id")

        recipe = bundle["recipe"]
        if not isinstance(recipe, dict) or set(recipe) != {"tool", "transforms", "response_template"}:
            raise CapabilityValidationError("recipe schema mismatch")
        if recipe["tool"] not in _ALLOWED_TOOLS or recipe["response_template"] not in _ALLOWED_TEMPLATES:
            raise CapabilityValidationError("unknown tool or response template")
        if not isinstance(recipe["transforms"], list):
            raise CapabilityValidationError("transforms must be a list")
        for transform in recipe["transforms"]:
            if not isinstance(transform, dict) or transform.get("op") not in _ALLOWED_TRANSFORMS:
                raise CapabilityValidationError("unknown transform")
            if set(transform) != {"op", "field", "value"} or transform["field"] not in {"category", "room", "online"}:
                raise CapabilityValidationError("invalid transform schema")

        permissions = bundle["permissions"]
        if not isinstance(permissions, dict) or set(permissions) != {"risk", "allowed_tools", "forbidden_tools"}:
            raise CapabilityValidationError("permissions schema mismatch")
        expected_risk = {
            "local_4b_device": "device_action", "local_4b_scene": "scene_action",
        }.get(primary, "read_only")
        if permissions["risk"] != expected_risk:
            raise CapabilityValidationError("capability risk and executor mismatch")
        if permissions["allowed_tools"] != [recipe["tool"]]:
            raise CapabilityValidationError("recipe tool is not exactly allowed")
        if set(permissions["allowed_tools"]) & set(permissions["forbidden_tools"]):
            raise CapabilityValidationError("tool is both allowed and forbidden")
        if primary == "local_9b_agent" and recipe["tool"] != "external_home.device_list":
            raise CapabilityValidationError("9B agent requested unsupported tool")

        delivery = bundle["delivery"]
        if not isinstance(delivery, dict) or set(delivery) != {"transition", "progress"}:
            raise CapabilityValidationError("delivery schema mismatch")
        if not all(isinstance(delivery[key], str) and 1 <= len(delivery[key].strip()) <= 80 for key in delivery):
            raise CapabilityValidationError("invalid delivery phrase")

        review = bundle["review"]
        if not isinstance(review, dict) or set(review) != {"minimum_samples", "fact_consistency", "tool_argument_accuracy"}:
            raise CapabilityValidationError("review schema mismatch")
        if not isinstance(review["minimum_samples"], int) or review["minimum_samples"] < 1:
            raise CapabilityValidationError("invalid minimum samples")
        if review["fact_consistency"] != 1.0 or review["tool_argument_accuracy"] != 1.0:
            raise CapabilityValidationError("read-only home facts require perfect review gates")
        return bundle

    def install_initial(self, bundle: dict[str, Any]) -> dict[str, Any]:
        value = self.validate(bundle)
        value["status"] = "active"
        path = self._path(self.active_root, value["id"])
        with self._locked(value["id"]):
            if path.exists():
                raise CapabilityValidationError("active capability already exists")
            _write_private(path, self._pointer(value, []))
        return value

    def active(self, capability_id: str) -> dict[str, Any] | None:
        path = self._path(self.active_root, capability_id)
        if not path.exists():
            return None
        value = _read_json(path)
        if not isinstance(value, dict):
            raise CapabilityValidationError("invalid active capability pointer")
        if frozenset(value) in {frozenset(_LEGACY_EXPECTED_KEYS), frozenset(_EXPECTED_KEYS)}:
            legacy = self.validate(value)
            with self._locked(capability_id):
                current = _read_json(path)
                if isinstance(current, dict) and frozenset(current) in {
                    frozenset(_LEGACY_EXPECTED_KEYS), frozenset(_EXPECTED_KEYS),
                }:
                    _write_private(path, self._pointer(legacy, []))
                    value = _read_json(path)
                else:
                    value = current
        if not isinstance(value, dict):
            raise CapabilityValidationError("invalid active capability pointer")
        return self._read_pointer(capability_id, value)

    def list_active(self) -> list[dict[str, Any]]:
        if not self.active_root.exists():
            return []
        return [
            value for path in sorted(self.active_root.glob("*.json"))
            if (value := self.active(path.stem)) is not None
        ]

    def list_selectable(self, selector_model: str) -> list[dict[str, Any]]:
        allowed_tiers = {
            "0.8b": {"08b_eligible"},
            "4b": {"08b_eligible", "4b_eligible"},
        }
        if selector_model not in allowed_tiers:
            raise CapabilityValidationError("unsupported selector model")
        return [
            bundle for bundle in self.list_active()
            if bundle["selector"]["tier"] in allowed_tiers[selector_model]
        ]

    def candidate(self, capability_id: str) -> dict[str, Any] | None:
        value = _read_json(self._path(self.candidate_root, capability_id))
        return value if isinstance(value, dict) else None

    def candidate_hash(self, capability_id: str) -> str:
        value = self.candidate(capability_id)
        if not isinstance(value, dict):
            raise CapabilityValidationError("candidate not found")
        return _canonical_hash(value)

    def discard_candidate(self, capability_id: str) -> dict[str, Any]:
        with self._locked(capability_id):
            path = self._path(self.candidate_root, capability_id)
            value = _read_json(path)
            if not isinstance(value, dict):
                raise CapabilityValidationError("candidate not found")
            path.unlink()
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return value

    def candidate_snapshot(self, capability_id: str) -> tuple[dict[str, Any], str]:
        """Return a copied candidate and the hash of that exact same read."""
        with self._locked(capability_id):
            value = _read_json(self._path(self.candidate_root, capability_id))
            if not isinstance(value, dict):
                raise CapabilityValidationError("candidate not found")
            return json.loads(json.dumps(value)), _canonical_hash(value)

    def list_candidates(self) -> list[dict[str, Any]]:
        if not self.candidate_root.exists():
            return []
        return [
            value for path in sorted(self.candidate_root.glob("*.json"))
            if isinstance((value := _read_json(path)), dict)
        ]

    def propose(self, bundle: dict[str, Any], evidence_ids: list[str]) -> dict[str, Any]:
        value = self.validate(bundle)
        capability_id = value["id"]
        with self._locked(capability_id):
            candidate_path = self._path(self.candidate_root, capability_id)
            if candidate_path.exists():
                raise CapabilityValidationError("candidate already exists")
            pointer = _read_json(self._path(self.active_root, capability_id))
            active = self._read_pointer(capability_id, pointer) if isinstance(pointer, dict) else None
            if not active or value["version"] != active["version"] + 1:
                raise CapabilityValidationError("candidate version must follow active version")
            if value["selector"]["tier"] != active["selector"]["tier"]:
                raise CapabilityValidationError("selector tier changes require explicit proposal")
            proposal = {
                "status": "proposed", "capability": value,
                "from_version": active["version"],
                "evidence_ids": [str(item)[:64] for item in evidence_ids[-100:]],
            }
            _write_private(candidate_path, proposal)
        return proposal

    def propose_tier_change(
        self, capability_id: str, target_tier: str, evidence_ids: list[str],
    ) -> dict[str, Any]:
        with self._locked(capability_id):
            candidate_path = self._path(self.candidate_root, capability_id)
            if candidate_path.exists():
                raise CapabilityValidationError("candidate already exists")
            pointer = _read_json(self._path(self.active_root, capability_id))
            active = self._read_pointer(capability_id, pointer) if isinstance(pointer, dict) else None
            if active is None:
                raise CapabilityValidationError("active capability not found")
            tiers = ["openclaw_only", "4b_eligible", "08b_eligible"]
            current_tier = active["selector"]["tier"]
            if target_tier not in tiers or target_tier == current_tier:
                raise CapabilityValidationError("invalid selector tier transition")
            current_index = tiers.index(current_tier)
            target_index = tiers.index(target_tier)
            if target_index > current_index + 1:
                raise CapabilityValidationError("selector tier upgrade cannot skip a level")
            candidate = json.loads(json.dumps(active))
            candidate["version"] = active["version"] + 1
            candidate["status"] = "proposed"
            candidate["selector"]["tier"] = target_tier
            proposal = {
                "status": "proposed", "capability": self.validate(candidate),
                "from_version": active["version"],
                "evidence_ids": [str(item)[:64] for item in evidence_ids[-100:]],
                "change_kind": "selector_tier",
                "from_tier": current_tier,
                "target_tier": target_tier,
            }
            _write_private(candidate_path, proposal)
        return proposal

    def propose_safety_downgrade(
        self, capability_id: str, evidence_ids: list[str],
    ) -> dict[str, Any]:
        """Safety incidents supersede a pending ordinary candidate under one lock."""
        with self._locked(capability_id):
            pointer = _read_json(self._path(self.active_root, capability_id))
            active = self._read_pointer(capability_id, pointer) if isinstance(pointer, dict) else None
            if active is None or active["selector"]["tier"] == "openclaw_only":
                raise CapabilityValidationError("safety downgrade is not applicable")
            evidence: list[str] = []
            for item in evidence_ids:
                value = str(item or "").strip()[:64]
                if value and value not in evidence:
                    evidence.append(value)
            if not evidence:
                raise CapabilityValidationError("safety downgrade needs evidence")
            candidate = json.loads(json.dumps(active))
            candidate["version"] = active["version"] + 1
            candidate["status"] = "proposed"
            candidate["selector"]["tier"] = "openclaw_only"
            proposal = {
                "status": "proposed", "capability": self.validate(candidate),
                "from_version": active["version"], "evidence_ids": evidence[-100:],
                "change_kind": "selector_tier",
                "from_tier": active["selector"]["tier"],
                "target_tier": "openclaw_only",
            }
            _write_private(self._path(self.candidate_root, capability_id), proposal)
            return proposal

    def replace_candidate_if_unchanged(
        self, bundle: dict[str, Any], evidence_ids: list[str], observed_hash: str,
    ) -> dict[str, Any]:
        """CAS replacement for explicit evidence merges; never changes selector tier."""
        value = self.validate(bundle)
        capability_id = value["id"]
        with self._locked(capability_id):
            candidate_path = self._path(self.candidate_root, capability_id)
            current = _read_json(candidate_path)
            if not isinstance(current, dict) or _canonical_hash(current) != observed_hash:
                raise CapabilityValidationError("candidate changed during merge")
            pointer = _read_json(self._path(self.active_root, capability_id))
            active = self._read_pointer(capability_id, pointer) if isinstance(pointer, dict) else None
            if not active or value["version"] != active["version"] + 1:
                raise CapabilityValidationError("candidate version must follow active version")
            if value["selector"]["tier"] != active["selector"]["tier"]:
                raise CapabilityValidationError("selector tier changes require explicit proposal")
            proposal = {
                "status": "proposed", "capability": value,
                "from_version": active["version"],
                "evidence_ids": [str(item)[:64] for item in evidence_ids[-100:]],
            }
            _write_private(candidate_path, proposal)
            return proposal

    def promote_with_report(
        self, capability_id: str, expected_version: int, report: dict[str, Any],
    ) -> dict[str, Any]:
        active = self.active(capability_id)
        proposal = self.candidate(capability_id)
        common = {
            "schema_version", "capability_id", "active_version", "active_hash",
            "candidate_version", "candidate_hash", "gates", "passed", "report_hash",
        }
        if not isinstance(report, dict) or report.get("schema_version") not in {1, 2}:
            raise CapabilityValidationError("validation report schema mismatch")
        required = (
            common
            if report["schema_version"] == 1
            else common | {"change_kind", "from_tier", "target_tier"}
        )
        if set(report) != required:
            raise CapabilityValidationError("validation report schema mismatch")
        body = {key: value for key, value in report.items() if key != "report_hash"}
        if report["report_hash"] != _canonical_hash(body):
            raise CapabilityValidationError("validation report hash mismatch")
        if not active or active["version"] != expected_version:
            raise CapabilityValidationError("stale active capability version")
        if not isinstance(proposal, dict) or proposal.get("from_version") != expected_version:
            raise CapabilityValidationError("candidate not found or stale")
        candidate = self.validate(proposal.get("capability"))
        if report["schema_version"] == 1:
            expected_gates = {"replay", "shadow", "security"}
            if proposal.get("change_kind") is not None:
                raise CapabilityValidationError("tier change requires layered validation report")
            if candidate["selector"]["tier"] != active["selector"]["tier"]:
                raise CapabilityValidationError("tier change requires layered validation report")
        else:
            if (
                report.get("change_kind") != "selector_tier"
                or proposal.get("change_kind") != "selector_tier"
                or report.get("from_tier") != proposal.get("from_tier")
                or report.get("target_tier") != proposal.get("target_tier")
                or report.get("from_tier") != active["selector"]["tier"]
                or report.get("target_tier") != candidate["selector"]["tier"]
            ):
                raise CapabilityValidationError("tier report is not bound to proposal")
            target = report["target_tier"]
            if target == "4b_eligible":
                expected_gates = {
                    "replay_4b", "dangerous_negatives_4b", "shadow", "security",
                }
            elif target == "08b_eligible":
                expected_gates = {
                    "replay_08b", "dangerous_negatives_08b", "shadow", "security",
                }
            elif target == "openclaw_only":
                expected_gates = {"security"}
            else:
                raise CapabilityValidationError("invalid tier validation target")
        gates = report.get("gates")
        if not isinstance(gates, dict) or set(gates) != expected_gates:
            raise CapabilityValidationError("validation gates schema mismatch")
        if report.get("passed") is not True or any(
            not isinstance(gates[name], dict) or gates[name].get("passed") is not True
            for name in expected_gates
        ):
            raise CapabilityValidationError("validation report did not pass")
        if (
            report.get("capability_id") != capability_id
            or report.get("active_version") != expected_version
            or report.get("active_hash") != _canonical_hash(active)
            or report.get("candidate_version") != candidate["version"]
            or report.get("candidate_hash") != _canonical_hash(candidate)
        ):
            raise CapabilityValidationError("validation report is not bound to current state")
        return self._promote_validated(
            capability_id, expected_version,
            expected_active_hash=report["active_hash"],
            expected_candidate_hash=report["candidate_hash"],
        )

    def _promote_validated(
        self, capability_id: str, expected_version: int,
        *, expected_active_hash: str | None = None,
        expected_candidate_hash: str | None = None,
    ) -> dict[str, Any]:
        candidate_path = self._path(self.candidate_root, capability_id)
        with self._locked(capability_id):
            pointer_path = self._path(self.active_root, capability_id)
            current_pointer = _read_json(pointer_path)
            if not isinstance(current_pointer, dict):
                raise CapabilityValidationError("invalid active capability pointer")
            active = self._read_pointer(capability_id, current_pointer)
            proposal = self.candidate(capability_id)
            if active["version"] != expected_version:
                raise CapabilityValidationError("stale active capability version")
            if not isinstance(proposal, dict) or proposal.get("from_version") != expected_version:
                raise CapabilityValidationError("candidate not found or stale")
            candidate = self.validate(proposal["capability"])
            if expected_active_hash is not None and _canonical_hash(active) != expected_active_hash:
                raise CapabilityValidationError("active changed after validation")
            if expected_candidate_hash is not None and _canonical_hash(candidate) != expected_candidate_hash:
                raise CapabilityValidationError("candidate changed after validation")
            candidate["status"] = "active"
            if not isinstance(current_pointer, dict) or set(current_pointer) != {
                "schema_version", "id", "version", "bundle_hash", "snapshot", "history",
            }:
                raise CapabilityValidationError("invalid active capability pointer")
            history = list(current_pointer["history"]) + [current_pointer["snapshot"]]
            _write_private(pointer_path, self._pointer(candidate, history))
        candidate_path.unlink(missing_ok=True)
        return candidate

    def rollback(self, capability_id: str) -> dict[str, Any]:
        with self._locked(capability_id):
            pointer_path = self._path(self.active_root, capability_id)
            pointer = _read_json(pointer_path)
            if not isinstance(pointer, dict) or not isinstance(pointer.get("history"), list) or not pointer["history"]:
                raise CapabilityValidationError("no rollback version")
            previous_path = self.root / pointer["history"][-1]
            previous = _read_json(previous_path)
            if not isinstance(previous, dict):
                raise CapabilityValidationError("rollback snapshot missing or corrupt")
            previous = self.validate(previous)
            remaining = pointer["history"][:-1]
            _write_private(pointer_path, self._pointer(previous, remaining))
            return previous

    def history(self, capability_id: str) -> list[dict[str, Any]]:
        pointer = _read_json(self._path(self.active_root, capability_id))
        if not isinstance(pointer, dict) or not isinstance(pointer.get("history"), list):
            return []
        result = []
        for relative in pointer["history"]:
            value = _read_json(self.root / relative)
            if isinstance(value, dict):
                result.append(value)
        return result
