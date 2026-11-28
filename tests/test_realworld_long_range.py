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
    envelope = plan["fw_turn_envelope"]
    assert envelope["px4_fw_bank_limit_deg"] == 35
    assert envelope["guided_ground_speed_mps"] == 15
    assert envelope["guided_loiter_radius_m"] == 80
    assert envelope["minimum_turn_radius_m"] == pytest.approx(64.23, abs=0.02)
    assert envelope["required_bank_at_radius_deg"] == pytest.approx(29.34, abs=0.02)
    assert plan["gimbal_stabilization"]["target_pitch_deg"] == -56
    assert envelope["gimbal_witness"]["joint_pitch_range_deg"] == [19, 44]
    scenario = flight.SCENARIO_ROOT / flight.DELTA / f"{profile}.scenario.json"
    mission = flight.load_mission(scenario)
    assert float(dict(flight.collection_plan(scenario, Path("out"), 7, mission).extra_sim_params)["FW_R_LIM"]) == 35


def test_delta_policy_reuses_guided_fw_and_mc_commands():
    targets = [(100, 200, -85), (300, -360, -85)]
    policy = flight.LongRangePolicy(targets, 80, vtol=True, fw_speed_mps=15, gimbal_pitch_deg=-56)
    actions = [policy.next_action() for _ in range(5)]
    assert [(a.kind, getattr(a, "mode", None)) for a in actions] == [
        ("reposition", "FW"), ("reposition", "FW"), ("transition", "MC"),
        ("mc_reposition", "MC"), ("stop", None),
    ]
    assert all(a.radius_m == 80 and a.speed_mps == 15 for a in actions[:2])
    assert policy.cruise_gimbal_target == {"roll": 0, "pitch": -56, "yaw": 0}
    assert actions[-2].target_ned == (0, 0, -85)
    assert actions[-2].speed_mps == 8


def test_delta_track_cpu_plan_preserves_repeated_world_targets(monkeypatch, capsys):
    from scripts import collect_sim_teacher_episodes as collector

    monkeypatch.delenv("MULTIDETECT_SIM_LISTEN_HOST", raising=False)
    monkeypatch.setattr(collector, "docker_command", lambda *_a, **_kw: pytest.fail("Docker planning invoked"))
    assert flight.main(["7", "--profile", flight.DEFAULT_PROFILE, "--plan-only",
                        "--gimbal-mode", "track", "--track-target", "42,-93,300",
                        "--track-target", "42.001,-93.002,302"]) == 0
    plan = json.loads(capsys.readouterr().out)
    refs = [{"lat_deg": 42., "lon_deg": -93., "alt_m": 300.},
            {"lat_deg": 42.001, "lon_deg": -93.002, "alt_m": 302.}]
    assert plan["gimbal_stabilization"]["mode"] == "track"
    assert plan["gimbal_stabilization"]["track_targets"] == refs
    assert plan["collector_plan"]["gimbal_mode"] == "track"
    assert plan["collector_plan"]["track_targets"] == refs


def test_delta_track_without_target_warns_and_falls_back_to_lock(monkeypatch, capsys, caplog):
    monkeypatch.delenv("MULTIDETECT_SIM_LISTEN_HOST", raising=False)
    assert flight.main(["7", "--profile", flight.DEFAULT_PROFILE, "--plan-only",
                        "--gimbal-mode", "track"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["gimbal_stabilization"]["mode"] == "lock"
    assert plan["collector_plan"]["gimbal_mode"] == "lock"
    assert "falling back to lock" in caplog.text


@pytest.mark.parametrize("value", ["42,-93", "nan,0,0", "91,0,0", "0,181,0", "1,2,3,4"])
def test_track_cli_rejects_invalid_world_targets(value):
    with pytest.raises(SystemExit):
        flight.main(["7", "--plan-only", "--track-target", value])


def test_collector_delegates_track_options_and_targets(monkeypatch):
    from scripts import collect_sim_teacher_episodes as collector

    calls = []
    monkeypatch.setattr(flight, "main", lambda argv: calls.append(argv) or 0)
    assert collector.main(["--long-range-profile", flight.DEFAULT_PROFILE, "--teacher", "explore",
                           "--episodes", "1", "--steps-per-episode", "10",
                           "--dry-run", "--gimbal-mode", "track", "--track-target", "42,-93,300",
                           "--track-target", "43,-94,301"]) == 0
    argv = calls[0]
    assert argv[argv.index("--gimbal-mode") + 1] == "track"
    assert [value.partition("=")[2] for value in argv if value.startswith("--track-target=")] == [
        "42.0,-93.0,300.0", "43.0,-94.0,301.0"]


def test_track_cli_supports_space_form_with_negative_latitude(monkeypatch, capsys):
    monkeypatch.delenv("MULTIDETECT_SIM_LISTEN_HOST", raising=False)
    assert flight.main(["7", "--profile", flight.DEFAULT_PROFILE, "--plan-only",
                        "--gimbal-mode", "track", "--track-target", "-35.3,149.1,600"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["gimbal_stabilization"]["track_targets"] == [
        {"lat_deg": -35.3, "lon_deg": 149.1, "alt_m": 600.}]


@pytest.mark.parametrize("pitch_deg", [-10, 0, 15])
@pytest.mark.parametrize("bank_deg", [-35, 0, 35])
def test_delta_turn_envelope_has_exact_horizon_witness(pitch_deg, bank_deg):
    from multidetect.camera_mount import _mat_mul
    from multidetect.sim_teacher.horizon_stabilization import c11_rotation_deg, rotation_deg

    fw = rotation_deg({"roll": bank_deg, "pitch": pitch_deg, "yaw": 63})
    body = _mat_mul(fw, rotation_deg({"roll": 0, "pitch": -90, "yaw": 0}))
    witness = {"roll": 0, "pitch": 34 - pitch_deg, "yaw": bank_deg}
    camera = _mat_mul(body, c11_rotation_deg(witness))
    assert camera == pytest.approx(rotation_deg({"roll": 0, "pitch": -56, "yaw": 63}))


@pytest.mark.parametrize(("radius", "bank", "pitch", "message"), [
    (80, 30, -56, "FW_R_LIM"),
    (80, 40, -56, "FW_R_LIM"),
    (60, 35, -56, "radius is too small"),
    (80, 35, -45, "FW pitch envelope"),
])
def test_delta_plan_rejects_turns_outside_gimbal_envelope(radius, bank, pitch, message):
    from types import SimpleNamespace

    profile = json.loads((flight.ROOT / "configs/airframes/delta_tailsitter_gimbal.json").read_text())
    with pytest.raises(ValueError, match=message):
        flight._delta_fw_turn_envelope(SimpleNamespace(loiter_radius_m=radius), profile, bank, pitch)


def test_delta_dem_audit_rejects_impossible_cruise_corridor():
    mission = flight.load_mission(flight.DEFAULT_SCENARIO)
    targets = [(north, east, down - 50) for north, east, down in mission["targets"]]
    with pytest.raises(ValueError, match="DEM route envelope"):
        flight._audit_delta_route(mission["geometry"], targets, mission["source"], mission["terrain"], 4000)


@pytest.mark.parametrize("confirmed", [True, False])
def test_retask_orbit_stops_offboard_only_after_fresh_auto_loiter(monkeypatch, confirmed):
    from types import SimpleNamespace

    from multidetect.sim_teacher.executor import ExecutionResult

    calls = []

    class FakeExecutor:
        vehicle = object()
        telemetry_reader = SimpleNamespace(state={})
        _clock = staticmethod(lambda: 1)
        _geo_reference = staticmethod(lambda: (None, (0, 0, 0)))
        _position = staticmethod(lambda: (200, 20, -85))

        def execute(self, command):
            calls.append(("execute", command.kind))
            return ExecutionResult("success", 0, ack={})

    streamer = SimpleNamespace(
        set_path_target=lambda target, tangent: calls.append(("path", target, tangent)),
        start=lambda: calls.append(("start",)), stop=lambda: calls.append(("stop",)),
    )

    def wait_mode(reader, mode, *, timeout_s, after_ns):
        calls.append(("mode", mode, timeout_s, after_ns))
        return {"confirmed": confirmed}

    fake = SimpleNamespace(_SmokeFlightPolicy=None, _wait_motion_command=None, FixedWingVtolExecutor=FakeExecutor,
                           SetpointStreamer=lambda _vehicle: streamer, _wait_for_offboard=lambda *a, **kw: None,
                           _flight_state_snapshot=lambda _: {"vtol_state": 4}, _wait_px4_mode=wait_mode)
    monkeypatch.setattr(flight, "_check_offboard_mode", lambda *_: None)
    monkeypatch.setattr(flight.time, "monotonic_ns", lambda: 123)
    with flight.collector_overrides(fake, {"geometry": None, "targets": []}, x1_heading_deg=0):
        executor = fake.FixedWingVtolExecutor()
        metadata = {"x1_retask_timeout_s": 60}
        approach = flight.MotionCommand("reposition", mode="FW", target_ned=(200, 120, -85),
                                        fly_through_radius_m=24, metadata=metadata)
        orbit = flight.MotionCommand("orbit", mode="FW", target_ned=(200, 120, -85), radius_m=65,
                                     duration_s=14, metadata=metadata)
        assert executor.execute(approach).status == "success"
        assert calls[:2] == [("path", (200, 120, -85), (0, 1)), ("start",)]
        if confirmed:
            assert executor.execute(orbit).ack["px4_orbit_mode"] == {"confirmed": True}
        else:
            with pytest.raises(RuntimeError, match="did not confirm AUTO.LOITER"):
                executor.execute(orbit)
        assert calls[2:] == [("execute", "orbit"), ("mode", flight.AUTO_LOITER_MODE, 5, 123)] + (
            [("stop",)] if confirmed else [])


def test_corridor_dogleg_offboard_tangents_chain_from_previous_target(monkeypatch):
    from types import SimpleNamespace

    from multidetect.sim_teacher.executor import ExecutionResult

    calls = []

    class FakeExecutor:
        vehicle = object()
        telemetry_reader = SimpleNamespace(state={})
        _clock = staticmethod(lambda: 1)
        _geo_reference = staticmethod(lambda: (None, (0, 0, 0)))
        _position = staticmethod(lambda: (0, 0, 0))

        def execute(self, command):
            return ExecutionResult("success", 0, ack={})

    streamer = SimpleNamespace(
        set_path_target=lambda target, tangent: calls.append((target, tangent)),
        start=lambda: calls.append(("start",)), stop=lambda: None)

    fake = SimpleNamespace(_SmokeFlightPolicy=None, _wait_motion_command=None, FixedWingVtolExecutor=FakeExecutor,
                           SetpointStreamer=lambda _vehicle: streamer, _wait_for_offboard=lambda *a, **kw: None,
                           _flight_state_snapshot=lambda _: {"vtol_state": 4})
    monkeypatch.setattr(flight, "_check_offboard_mode", lambda *_: None)
    monkeypatch.setattr(flight.time, "monotonic_ns", lambda: 123)
    mission = {"geometry": None, "targets": [], "corridor_lowering": object()}
    with flight.collector_overrides(fake, mission, x1_heading_deg=0):
        executor = fake.FixedWingVtolExecutor()
        for target in ((300, 0, -47), (555, 190, -47), (0, 0, -47)):
            command = flight.MotionCommand("reposition", mode="FW", target_ned=target,
                                           fly_through_radius_m=25, metadata={})
            assert executor.execute(command).status == "success"

    paths = [call for call in calls if call[0] != "start"]
    assert paths[0] == ((300, 0, -47), (1.0, 0.0))
    first = math.hypot(255, 190)
    assert paths[1][1] == pytest.approx((255 / first, 190 / first))
    home = math.hypot(555, 190)
    assert paths[2][0] == (0, 0, -47)
    assert paths[2][1] == pytest.approx((-555 / home, -190 / home))
