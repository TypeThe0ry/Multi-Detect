from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from scripts.realworld import long_range_flight as flight


def test_sidecar_name_and_target_distance_contract(tmp_path: Path) -> None:
    scenario = tmp_path / "demo.scenario.json"
    assert flight.sidecar_path(scenario).name == "demo.long_range.json"


def test_w6km_profile_preflight_binds_real_terrain_fixed_wing_and_rtf_gate() -> None:
    scenario = Path(
        "configs/scenarios/realworld/rc_cessna_mono_cam/"
        "rw_farm_iowa_w6km__clear_noon.scenario.json"
    )
    mission = flight.load_mission(scenario)
    assert mission["profile"] == "rw_farm_iowa_w6km__clear_noon"
    assert mission["minimum_max_distance_m"] >= 4000.0
    assert mission["minimum_rtf"] == pytest.approx(0.9)
    assert flight.output_name(scenario) == "rw_farm_iowa_w6km__clear_noon__long-range__rtf90"


def test_w6km_profile_rejects_lower_requested_rtf() -> None:
    scenario = Path(
        "configs/scenarios/realworld/rc_cessna_mono_cam/"
        "rw_farm_iowa_w6km__clear_noon.scenario.json"
    )
    with pytest.raises(ValueError, match="RTF requirement"):
        flight.load_mission(scenario, minimum_rtf=0.89)


def test_w2km_sidecar_writer_keeps_1500m_range_and_rtf_gate(monkeypatch, tmp_path: Path) -> None:
    scenario = Path(
        "configs/scenarios/realworld/rc_cessna_mono_cam/"
        "rw_farm_iowa_w2km__clear_noon.scenario.json"
    )
    sidecar = tmp_path / "rw_farm_iowa_w2km__clear_noon.long_range.json"
    monkeypatch.setattr(flight, "sidecar_path", lambda _scenario: sidecar)

    flight.write_mission_sidecar(scenario, extent_m=2000.0)
    mission = flight.load_mission(scenario)

    assert mission["source"]["extent_m"] == 2000.0
    assert mission["minimum_max_distance_m"] == pytest.approx(1500.0)
    assert mission["planned_max_distance_m"] >= 1500.0
    assert mission["minimum_rtf"] == pytest.approx(0.9)


def test_w2km_derived_runtime_heading_matches_mission() -> None:
    scenario = Path(
        "configs/scenarios/realworld/rc_cessna_mono_cam/"
        "rw_farm_iowa_w2km__clear_noon.scenario.json"
    )
    document, runtime = flight._scenario_document(scenario)
    mission = flight.load_mission(scenario)
    yaw = float(runtime["vehicle"]["spawn_pose"]["yaw_rad"])
    heading = mission["geometry"].takeoff_heading_deg

    assert document["scenario_id"] == "rw_farm_iowa_w2km__clear_noon"
    assert math.cos(yaw) == pytest.approx(math.sin(math.radians(heading)), abs=1e-5)
    assert math.sin(yaw) == pytest.approx(math.cos(math.radians(heading)), abs=1e-5)


@pytest.mark.parametrize("airframe,profile", [(flight.AIRFRAME, flight.DEFAULT_PROFILE),
                                              (flight.DELTA, flight.PROFILES[1])])
def test_collector_profile_entry_delegates_without_starting_runtime(monkeypatch, airframe, profile) -> None:
    from scripts import collect_sim_teacher_episodes as collector

    calls: list[list[str]] = []

    def plan_only(argv: list[str]) -> int:
        calls.append(argv)
        return 17

    monkeypatch.setattr(flight, "main", plan_only)
    result = collector.main(
        [
            "--long-range-profile",
            profile,
            "--airframe",
            airframe,
            "--teacher",
            "explore",
            "--episodes",
            "1",
            "--steps-per-episode",
            "10",
            "--dry-run",
        ]
    )
    assert result == 17
    scenario = flight.SCENARIO_ROOT / airframe / f"{profile}.scenario.json"
    assert calls == [
        [
            "7",
            str(scenario.resolve()),
            str(flight.default_output_path(scenario, 0.9).resolve()),
            "--min-rtf",
            "0.9",
            "--airframe",
            airframe,
            "--plan-only",
        ]
    ]


def test_long_range_policy_emits_waypoints_then_stop() -> None:
    policy = flight.LongRangePolicy([(1.0, 2.0, -100.0), (3.0, 4.0, -100.0)], 80.0)
    first = policy.next_action()
    second = policy.next_action()
    final = policy.next_action()
    assert first.kind == "reposition"
    assert first.target_ned == (1.0, 2.0, -100.0)
    assert first.radius_m == 80.0
    assert second.target_ned == (3.0, 4.0, -100.0)
    assert final.kind == "stop"


def test_bind_target_adds_live_home_local_ned() -> None:
    action = flight.MotionCommand("reposition", target_ned=(100.0, -50.0, -80.0), radius_m=80.0)
    bound = flight.bind_target(action, (12.0, 7.0, 3.0))
    assert bound.target_ned == (112.0, -43.0, -77.0)


def test_load_mission_rejects_short_route_before_runtime(tmp_path: Path) -> None:
    scenario = tmp_path / "short.scenario.json"
    sidecar = flight.sidecar_path(scenario)
    sidecar.write_text(json.dumps({"schema_version": 1, "extent_m": 6000,
                                   "mission_compile_runtime": {}, "task": {}, "context": {},
                                   "minimum_max_distance_m": 4000}))
    with pytest.raises((KeyError, ValueError, TypeError)):
        flight.load_mission(scenario)


def test_plan_only_requires_reserved_slot_count(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MULTIDETECT_SITL_SLOTS", "7")
    with pytest.raises(SystemExit):
        flight.main(["7", "missing.scenario.json", str(tmp_path), "--plan-only"])


@pytest.mark.parametrize("profile", flight.PROFILES)
def test_delta_cpu_plan_compiles_vtol_lifecycle_and_dem_envelope(monkeypatch, capsys, profile):
    from scripts import collect_sim_teacher_episodes as collector

    monkeypatch.delenv("MULTIDETECT_SIM_LISTEN_HOST", raising=False)
    monkeypatch.setattr(collector, "docker_command", lambda *_a, **_kw: pytest.fail("Docker planning invoked"))
    assert flight.main(["7", "--profile", profile, "--plan-only"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["flight_started"] is False
    assert plan["airframe"] == flight.DELTA
    assert plan["minimum_rtf"] == 0.9
    assert plan["planned_max_distance_m"] > plan["minimum_max_distance_m"] == 4000
    assert 40 <= plan["planned_agl_envelope_m"][0] <= plan["planned_agl_envelope_m"][1] <= 120
    assert 250 <= math.hypot(*plan["fw_waypoints_home_ned_m"][-1][:2]) <= 650
    wire = plan["compiled_mission"]["compiled_mission"]["wire_items"]
    assert [item["command"] for item in wire] == [84, 3000, *([16] * 7), 19, 3000, 85]
    assert [wire[1]["param1"], wire[-2]["param1"]] == [4, 3]
    collector_plan = plan["collector_plan"]
    assert collector_plan["takeoff_altitude_m"] == 65
    assert collector_plan["video_hz"] == pytest.approx(1 / 6)
    assert collector_plan["video_use_sim_time"] is True
    assert collector_plan["smoke_flight"] is False


def test_delta_policy_reuses_guided_fw_and_mc_commands():
    targets = [(100, 200, -85), (300, -360, -85)]
    policy = flight.LongRangePolicy(targets, 80, vtol=True)
    actions = [policy.next_action() for _ in range(5)]
    assert [(a.kind, getattr(a, "mode", None)) for a in actions] == [
        ("reposition", "FW"), ("reposition", "FW"), ("transition", "MC"),
        ("mc_reposition", "MC"), ("stop", None),
    ]
    assert actions[-2].target_ned == (0, 0, -85)


def test_delta_dem_audit_rejects_impossible_cruise_corridor():
    mission = flight.load_mission(flight.DEFAULT_SCENARIO)
    targets = [(north, east, down - 50) for north, east, down in mission["targets"]]
    with pytest.raises(ValueError, match="DEM route envelope"):
        flight._audit_delta_route(mission["geometry"], targets, mission["source"], mission["terrain"], 4000)
