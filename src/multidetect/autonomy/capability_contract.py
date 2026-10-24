"""Planning-time aircraft capability contract.

The authoritative source is the onboard configuration plus the resolved
airframe profile.  A mission package may remove capabilities, never grant
ones the aircraft does not have.  Keep the skill requirements here so the
planner, package preflight and tests use one table.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SKILL_REQUIRED_CAPABILITIES: dict[str, frozenset[str]] = {
    "deliver": frozenset({"payload_drop"}),
    "hover": frozenset({"hover"}),
}

KNOWN_CAPABILITIES = frozenset(
    {"payload_drop", "winch", "spotlight", "speaker", "landing_on_target", "hover"}
)


def normalize_capabilities(values: Any) -> frozenset[str]:
    """Normalize configured/package capability names, retaining legacy payload."""
    if values is None:
        return frozenset()
    if isinstance(values, Mapping):
        values = [key for key, enabled in values.items() if enabled]
    if isinstance(values, (str, bytes)):
        values = [values]
    result = {str(value).strip() for value in values if str(value).strip()}
    if "payload" in result:
        result.add("payload_drop")
    return frozenset(result)


def capabilities_for_airframe(profile: Any, configured: Any = ()) -> frozenset[str]:
    """Resolve physical capabilities from config and an AirframeProfile."""
    result = set(normalize_capabilities(configured))
    if getattr(profile, "can_hover", False):
        result.add("hover")
    if getattr(profile, "category", None) == "fixed_wing":
        result.add("airframe:fixed_wing")
    landing = getattr(getattr(profile, "landing", None), "methods", ())
    if getattr(profile, "can_hover", False) and any(
        method in {"multirotor", "vtol"} for method in landing
    ):
        result.add("landing_on_target")
    return frozenset(result)


def effective_capabilities(airframe: Any, package: Any = None) -> frozenset[str]:
    """Apply package declarations as a tightening intersection."""
    base = normalize_capabilities(airframe)
    if package is None:
        return base
    declared = normalize_capabilities(package)
    return frozenset(base & declared)


def capability_violations(plan: Any, capabilities: Any, *, airframe: Any = None) -> list[dict[str, Any]]:
    """Return stable planner violations for physically unsupported skills."""
    available = normalize_capabilities(capabilities)
    findings: list[dict[str, Any]] = []
    steps = plan.get("steps", ()) if isinstance(plan, Mapping) else getattr(plan, "steps", ())
    for step in steps:
        skill = step.get("skill") if isinstance(step, Mapping) else getattr(step, "skill", None)
        params = step.get("params", {}) if isinstance(step, Mapping) else getattr(step, "params", {})
        step_id = step.get("id") if isinstance(step, Mapping) else getattr(step, "id", None)
        required = set(SKILL_REQUIRED_CAPABILITIES.get(skill, ()))
        if skill == "hover" and airframe is None and "airframe:fixed_wing" not in available:
            required.discard("hover")
        if skill == "land" and params.get("target_ref") is not None and (
            airframe is not None or "airframe:fixed_wing" in available
        ):
            required.add("landing_on_target")
        missing = sorted(required - available)
        if missing:
            for capability in missing:
                findings.append(
                    {
                        "code": "missing_capability",
                        "step_id": step_id,
                        "message_zh": f"步骤 {step_id or ''} 使用 {skill}，缺少能力 {capability}",
                        "message_en": f"step {step_id or ''} uses {skill}; missing capability {capability}",
                        "message": f"缺少能力 {capability} / missing capability {capability}",
                        "blocking": True,
                    }
                )
    return findings


__all__ = [
    "KNOWN_CAPABILITIES",
    "SKILL_REQUIRED_CAPABILITIES",
    "capabilities_for_airframe",
    "capability_violations",
    "effective_capabilities",
    "normalize_capabilities",
]
