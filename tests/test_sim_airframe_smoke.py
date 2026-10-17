from __future__ import annotations

import json

import pytest
from pathlib import Path
from types import SimpleNamespace

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


def test_hand_and_catapult_profiles_apply_only_verified_launch_parameters() -> None:
    assert collector._profile_launch_parameters("rc_cessna_mono_cam", "hand") == (
        ("FW_LAUN_DETCN_ON", "1"),
        ("FW_LAUN_AC_THLD", "30"),
        ("FW_LAUN_AC_T", "0.05"),
    )
    assert collector._profile_launch_parameters("rc_cessna_mono_cam", "catapult") == (
        ("FW_LAUN_DETCN_ON", "1"),
        ("FW_LAUN_AC_THLD", "30"),
        ("FW_LAUN_AC_T", "0.05"),
    )
    assert collector._profile_launch_parameters("rc_cessna_mono_cam", "runway") == ()


def test_launch_force_is_forward_with_method_specific_impulse() -> None:
    hand = collector._launch_force("hand", 0.0)
    catapult = collector._launch_force("catapult", 0.0)
    assert hand["delta_v_mps"] == 12.0 and hand["duration_s"] == 0.25
    assert catapult["delta_v_mps"] == 17.0 and catapult["duration_s"] == 0.15
    assert hand["force_n"] == 1.7 * 12.0 / 0.25
    assert catapult["force_n"] == 1.7 * 17.0 / 0.15
    assert hand["y"] > 0.0 and abs(hand["x"]) < 1e-6
    assert hand["z"] > 0.0


def test_launch_force_uses_one_persistent_topic_exec_and_clear(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="published\ncleared\n", stderr="")

    monkeypatch.setattr(collector.subprocess, "run", fake_run)
    result = collector._apply_gazebo_launch(
        SimpleNamespace(), SimpleNamespace(name="codex-launch-0"),
        "rc_cessna_mono_cam_0", 0.0, "hand",
    )
    assert len(calls) == 1
    command = calls[0][0]
    assert command[0:2] == ["docker", "exec"] or command[1] == "exec"
    assert command[-4:-1] == ["codex-launch-0", "bash", "-c"]
    env = dict(item.split("=", 1) for item in command[2:-4] if item != "-e")
    # Force sits under `wrench`; both messages keep the quoted scoped link name.
    assert env["MD_WRENCH"].startswith('entity { name: "rc_cessna_mono_cam_0::base_link" type: LINK } wrench { force {')
    assert env["MD_CLEAR"] == 'name: "rc_cessna_mono_cam_0::base_link" type: LINK'
    script = command[-1]
    assert "/world/default/wrench/persistent" in script and "/world/default/wrench/clear" in script
    # The pulse and the delayed clear start together, so start-up cost cancels out.
    assert '& (sleep 0.250; gz topic -t /world/default/wrench/clear' in script
    assert result["force_n"] == 81.6


def test_launch_force_fails_when_gz_cannot_build_the_message(monkeypatch) -> None:
    monkeypatch.setattr(collector.subprocess, "run", lambda command, **kwargs: SimpleNamespace(
        returncode=0, stdout="", stderr="Unable to create message of type[gz.msgs.EntityWrench]"))
    with pytest.raises(RuntimeError, match="launch wrench failed"):
        collector._apply_gazebo_launch(SimpleNamespace(), SimpleNamespace(name="c"),
                                       "rc_cessna_mono_cam_0", 0.0, "hand")


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
    cessna = collector.CollectionPlan(**base, airframe="rc_cessna_mono_cam")
    runtime = {"vehicle": {"model": "px4_fixed_wing_standard", "airframe_profile_id": "px4_fixed_wing_gazebo_v1"}}
    assert collector._gazebo_model_name(cessna, runtime) == "rc_cessna_mono_cam_0"
