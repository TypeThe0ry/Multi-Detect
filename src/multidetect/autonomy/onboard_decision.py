"""Validated, non-executing proposals from an onboard slow vision model.

The caller binds a proposal to a task, observation and track before presenting
it to the planner. This document grants no actuator or MAVLink authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from multidetect.autonomy.serialization import (
    AutonomyContractError,
    require_real,
    require_text,
    strict_json_loads,
    strict_object,
)

DECISIONS = (
    "search_region",
    "approach_track",
    "confirm_target",
    "reject_track",
    "follow_track",
    "detour",
    "hover",
    "record",
    "return_home",
    "ask_clarify",
)
TRACK_DECISIONS = frozenset(
    {"approach_track", "confirm_target", "reject_track", "follow_track"}
)

DECISION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "args", "reason"],
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "args": {
            "type": "object",
            "additionalProperties": False,
            "required": ["track_id", "region", "duration_s"],
            "properties": {
                "track_id": {"type": ["string", "null"], "maxLength": 96},
                "region": {"type": ["string", "null"], "maxLength": 96},
                "duration_s": {"type": ["number", "null"], "minimum": 0, "maximum": 30},
            },
        },
        "reason": {"type": "string", "minLength": 1, "maxLength": 120},
    },
}


@dataclass(frozen=True, slots=True)
class DecisionArgs:
    track_id: str | None
    region: str | None
    duration_s: float | None


@dataclass(frozen=True, slots=True)
class OnboardDecision:
    decision: str
    args: DecisionArgs
    reason: str


def _optional_short_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    result = require_text(value, name)
    if len(result) > 96:
        raise AutonomyContractError(f"{name} must be at most 96 characters")
    return result


def parse_onboard_decision(raw: str | bytes | Mapping[str, Any]) -> OnboardDecision:
    """Reject malformed proposals before any task or safety interpretation."""
    document = (
        strict_json_loads(raw, name="onboard decision")
        if isinstance(raw, (str, bytes))
        else raw
    )
    obj = strict_object(
        document, name="onboard decision", required=frozenset({"decision", "args", "reason"})
    )
    decision = require_text(obj["decision"], "decision")
    if decision not in DECISIONS:
        raise AutonomyContractError(f"unsupported decision {decision!r}")
    args_obj = strict_object(
        obj["args"],
        name="args",
        required=frozenset({"track_id", "region", "duration_s"}),
    )
    track_id = _optional_short_text(args_obj["track_id"], "args.track_id")
    region = _optional_short_text(args_obj["region"], "args.region")
    duration = args_obj["duration_s"]
    duration_s = (
        None
        if duration is None
        else require_real(duration, "args.duration_s", minimum=0, maximum=30)
    )
    if decision in TRACK_DECISIONS and track_id is None:
        raise AutonomyContractError(f"{decision} requires args.track_id")
    if decision == "search_region" and region is None:
        raise AutonomyContractError("search_region requires args.region")
    reason = require_text(obj["reason"], "reason")
    if len(reason) > 120 or len(reason.split()) > 20:
        raise AutonomyContractError("reason must be at most 20 words and 120 characters")
    return OnboardDecision(decision, DecisionArgs(track_id, region, duration_s), reason)


# The slow model is reliable at identity ("is this the task's object?") but
# cannot measure apparent size from normalized coordinates. approach_track and
# confirm_target therefore both mean "identity matches"; the host, which knows
# the candidate's pixel size, decides whether it is close enough to confirm.
IDENTITY_MATCH = frozenset({"approach_track", "confirm_target"})


def identity_of(decision: str) -> str | None:
    """Return ``"match"``, ``"no_match"`` or ``None`` for non-identity decisions."""
    if decision in IDENTITY_MATCH:
        return "match"
    if decision == "reject_track":
        return "no_match"
    return None


def gate_confirm_by_size(
    decision: OnboardDecision, *, candidate_min_side_px: float, confirm_min_side_px: float
) -> OnboardDecision:
    """Host-side size gate: an identity match confirms only when large enough."""
    if decision.decision not in IDENTITY_MATCH:
        return decision
    if not candidate_min_side_px >= 0 or not confirm_min_side_px > 0:
        raise AutonomyContractError("pixel sizes must be finite and non-negative")
    gated = "confirm_target" if candidate_min_side_px >= confirm_min_side_px else "approach_track"
    if gated == decision.decision:
        return decision
    return OnboardDecision(gated, decision.args, decision.reason)


__all__ = [
    "DECISIONS",
    "DECISION_JSON_SCHEMA",
    "IDENTITY_MATCH",
    "DecisionArgs",
    "OnboardDecision",
    "gate_confirm_by_size",
    "identity_of",
    "parse_onboard_decision",
]
