from __future__ import annotations

import json
from pathlib import Path

from scripts import collect_sim_teacher_episodes as collector
from scripts import smoke_airframe_flight as smoke


ROOT = Path(__file__).resolve().parents[1]


def test_x500_plan_keeps_legacy_airframe_default() -> None:
    plan = collector.CollectionPlan("scenario", "runtime", "truth", "explore", 1, 1, 0, "out")
    assert plan.airframe == "x500_mono_cam"


def test_non_multirotor_runtime_is_accepted_for_explicit_airframe() -> None:
    runtime = ROOT / "configs/scenarios/airframes/advanced_plane_mono_cam_001.runtime.json"
    data = collector._validate_runtime(runtime, "advanced_plane_mono_cam")
    assert data["vehicle"]["model"] == "advanced_plane_mono_cam"


def test_smoke_plan_has_vtol_transition_and_airframe_speed_gate() -> None:
    profile = smoke.profile_for("standard_vtol_mono_cam")
    scenario = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"
    result = smoke.build_plan("standard_vtol_mono_cam", profile, scenario, None)
    stages = [step["stage"] for step in result["steps"]]
    assert stages == ["preflight", "takeoff", "transition_fw", "loiter", "reposition", "rtl", "transition_mc", "land"]
    assert result["frame_alignment"]["max_age_s"] == 0.25


def test_smoke_main_dry_run_is_json(capsys) -> None:
    assert smoke.main(["--airframe", "advanced_plane_mono_cam", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "dry_run"
    assert payload["takeoff_method"] == "runway"


def test_x500_subscribes_to_the_spawned_model_not_the_runtime_airframe_name() -> None:
    # r15 runtime channels name the abstract airframe; the spawned Gazebo model is x500_mono_cam.
    runtime = {"vehicle": {"model": "px4_multirotor_standard"}}
    base = dict(scenario="s.json", runtime="r.json", truth="t.json", teacher="expert_search",
                episodes=1, steps_per_episode=1, seed=0, out="o")
    x500 = collector.CollectionPlan(**base)
    assert collector._gazebo_model_name(x500, runtime) == "x500_mono_cam_0"
    plane = collector.CollectionPlan(**base, airframe="advanced_plane")
    assert collector._gazebo_model_name(plane, {"vehicle": {"model": "advanced_plane_mono_cam"}}) == "advanced_plane_mono_cam_0"
