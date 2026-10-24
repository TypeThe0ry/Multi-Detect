from __future__ import annotations

import json

from multidetect.autonomy.airframe_profile import load_airframe_profile
from multidetect.autonomy.capability_contract import (
    capabilities_for_airframe,
    capability_violations,
    effective_capabilities,
)
from multidetect.autonomy.mission_planner import compile_mission_plan, plan_prompt


def _deliver():
    return {
        "plan_version": 1,
        "goal": "deliver",
        "steps": [
            {"id": "d", "skill": "deliver", "params": {"target_ref": "poi:gate", "payload_ref": "payload:box"}, "on_success": "h", "on_fail": "h"},
            {"id": "h", "skill": "return_home", "params": {}, "on_success": None, "on_fail": None},
        ],
        "report": [],
    }


def test_missing_payload_is_rejected_and_reported():
    raw = json.dumps(_deliver())
    plan, report = compile_mission_plan(lambda *_: raw, "deliver to the gate", refs=("poi:gate",))
    assert plan is None
    assert "缺少能力 payload_drop" in report["intent_violations"][0]["message"]


def test_payload_capability_allows_delivery_and_package_only_tightens():
    raw = json.dumps(_deliver())
    plan, report = compile_mission_plan(lambda *_: raw, "deliver to the gate", refs=("poi:gate",), capabilities=frozenset({"payload_drop"}))
    assert plan is not None and report["error"] is None
    assert effective_capabilities({"payload_drop", "winch"}, {"payload_drop"}) == {"payload_drop"}


def test_fixed_wing_cannot_hover_or_land_on_target():
    profile = load_airframe_profile("configs/airframes/advanced_plane.json")
    capabilities = capabilities_for_airframe(profile)
    assert "hover" not in capabilities
    assert capability_violations({"steps": [{"id": "s", "skill": "hover", "params": {}}]}, capabilities)
    assert capability_violations({"steps": [{"id": "s", "skill": "land", "params": {"target_ref": "poi:pad"}}]}, capabilities)


def test_prompt_lists_nonempty_capabilities():
    assert "Available aircraft capabilities: payload_drop." in plan_prompt("deliver", capabilities=frozenset({"payload_drop"}))
