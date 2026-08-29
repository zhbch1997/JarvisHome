"""Build complete, versioned capability proposals from bounded route reviews."""
from __future__ import annotations

import json
from typing import Any

from capability_registry import CapabilityRegistry
from feedback_loop import RouteReview


def propose_from_review(
    registry: CapabilityRegistry,
    turn: dict[str, Any],
    review: RouteReview,
) -> dict[str, Any] | None:
    capability_id = str(review.capability or "none")
    if capability_id == "none":
        return None
    active = registry.active(capability_id)
    if active is None:
        return None

    try:
        existing, observed_hash = registry.candidate_snapshot(capability_id)
    except ValueError:
        existing = None
        observed_hash = None

    candidate = json.loads(json.dumps(active))
    candidate["version"] = active["version"] + 1
    candidate["status"] = "proposed"
    evidence_ids = []
    if (
        isinstance(existing, dict)
        and existing.get("from_version") == active["version"]
        and isinstance(existing.get("capability"), dict)
    ):
        existing_bundle = existing["capability"]
        existing_routing = existing_bundle.get("routing") or {}
        for example in existing_routing.get("examples") or []:
            if example not in candidate["routing"]["examples"]:
                candidate["routing"]["examples"].append(example)
        evidence_ids = list(existing.get("evidence_ids") or [])

    request = str(turn.get("request") or "").strip()[:200]
    examples = candidate["routing"]["examples"]
    if request and request not in examples:
        examples.append(request)
    evidence_id = str(turn.get("turn_id") or "").strip()[:64]
    if evidence_id and evidence_id not in evidence_ids:
        evidence_ids.append(evidence_id)
    if observed_hash is not None:
        return registry.replace_candidate_if_unchanged(
            candidate, evidence_ids, observed_hash,
        )
    return registry.propose(candidate, evidence_ids)
