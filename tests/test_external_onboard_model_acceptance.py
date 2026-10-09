from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from multidetect.autonomy import GeoPoint, OnboardTaskAction
from scripts.gazebo_capture_plan import DEFAULT_OUTPUT
from scripts.run_external_onboard_model_acceptance import (
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_REFERENCE,
    DEFAULT_MODEL_VERSION,
    NOMINAL_FLIGHT_TIME_S,
    PX4_AUTO_MISSION_CUSTOM_MODE,
    DisposableSitlAutonomousLifecycle,
    DockerSimulatorAcceptanceError,
    ExternalOnboardDockerProbe,
    _context,
    _scenario_seed_from_runtime,
    run_acceptance,
)


class _FakeMav:
    def __init__(self) -> None:
        self.commands: list[tuple[object, ...]] = []

    def command_long_send(self, *args: object) -> None:
        self.commands.append(tuple(args))

    def set_mode_send(self, *args: object) -> None:
        self.commands.append(tuple(args))


class _FakeConnection:
    target_system = 1
    target_component = 1

    def __init__(self) -> None:
        self.mav = _FakeMav()
        self.modes: list[str] = []
        self.heartbeats = [
            {
                "type": "HEARTBEAT",
                "base_mode": 1,
                "custom_mode": PX4_AUTO_MISSION_CUSTOM_MODE,
            },
            {"type": "HEARTBEAT", "base_mode": 128},
            {"type": "HEARTBEAT", "base_mode": 129, "custom_mode": (4 << 16) | (5 << 24)},
        ]

    def mode_mapping(self):
        return {"RTL": (29, 4, 5), "POSCTL": (81, 3, 0), "LAND": (29, 4, 6)}

    def set_mode(self, mode: str) -> None:
        self.modes.append(mode)

    def recv_match(self, **_kwargs: object) -> object | None:
        return self.heartbeats.pop(0) if self.heartbeats else None


class _FakeMavutil:
    class mavlink:
        MAV_MODE_FLAG_SAFETY_ARMED = 128
        MAV_CMD_COMPONENT_ARM_DISARM = 400


def test_disposable_sitl_lifecycle_is_explicit_and_idempotent() -> None:
    connection = _FakeConnection()
    lifecycle = DisposableSitlAutonomousLifecycle(
        connection,
        mavutil_module=_FakeMavutil,
        clock=lambda: 0.0,
        timeout_s=1.0,
    )

    lifecycle.start(object(), object(), object())
    lifecycle.start(object(), object(), object())
    lifecycle.request_return(object(), object())
    lifecycle.request_return(object(), object())
    lifecycle.report(object())

    assert lifecycle.mode_events == ["AUTO.MISSION", "RTL"]
    assert len(connection.mav.commands) == 3
    assert lifecycle.started is True
    assert lifecycle.armed_observed is True
    assert lifecycle.return_requested is True
    assert lifecycle.report_committed is True
    assert lifecycle.evidence()["physical_output_enabled"] is False


def test_external_dry_run_defaults_to_hash_bound_current_reference(tmp_path) -> None:
    evidence = tmp_path / "external-default-reference.json"

    result = run_acceptance(evidence_path=evidence)

    assert result["status"] == "planned_not_executed"
    assert result["model_id"] == DEFAULT_MODEL_ID
    assert result["model_version"] == DEFAULT_MODEL_VERSION
    assert result["model_sha256"] == (
        "727d0e71834311d8aa51dfe072e08ec249062b1945d9501394f5d89ad7a16ee6"
    )
    assert DEFAULT_MODEL_REFERENCE.is_file()
    assert result["external_gazebo_software_sim"] is False


def test_scenario_seed_is_resolved_from_hash_bound_runtime() -> None:
    scenario = (
        DEFAULT_MODEL_REFERENCE.parents[1]
        / "scenarios"
        / "urban_inspection_multirotor_001.scenario.json"
    )
    document = json.loads(scenario.read_text(encoding="utf-8"))
    assert _scenario_seed_from_runtime(scenario, document) == 71041


def test_external_dry_run_plans_recording_binding_after_real_capture(tmp_path) -> None:
    result = run_acceptance(
        evidence_path=tmp_path / "recording-plan.json",
        sensor_frame_count=2,
        sensor_frame_output_dir=tmp_path / "rgb",
        sensor_depth_output_dir=tmp_path / "depth",
        recording_output_directory=tmp_path / "recording",
        recording_id="planned-recording",
        recording_scenario_seed=17,
    )

    assert result["status"] == "planned_not_executed"
    assert result["recording_binding"] == {
        "requested": True,
        "status": "planned_after_capture",
        "output_directory": str((tmp_path / "recording").resolve()),
    }
    assert not (tmp_path / "recording").exists()


def test_recording_binding_rejects_missing_capture_directories(tmp_path) -> None:
    with pytest.raises(DockerSimulatorAcceptanceError, match="sensor-frame-output-dir"):
        run_acceptance(
            evidence_path=tmp_path / "recording-plan.json",
            sensor_frame_count=2,
            recording_output_directory=tmp_path / "recording",
        )


def test_external_dry_run_binds_offline_capture_plan_without_runtime_truth(tmp_path) -> None:
    result = run_acceptance(
        evidence_path=tmp_path / "external-capture-plan.json",
        capture_plan_path=DEFAULT_OUTPUT,
    )

    assert result["status"] == "planned_not_executed"
    assert result["capture_plan"]["scenario_id"] == "urban_inspection_multirotor_001"
    assert result["capture_plan"]["target_pose_excluded_from_runtime"] is True
    assert result["capture_plan"]["target_pose_excluded_from_model_input"] is True


def test_disposable_sitl_restarts_replaced_mission_without_rearming() -> None:
    connection = _FakeConnection()
    lifecycle = DisposableSitlAutonomousLifecycle(
        connection,
        mavutil_module=_FakeMavutil,
        clock=lambda: 0.0,
        timeout_s=1.0,
    )

    lifecycle.start(object(), object(), object())
    connection.heartbeats.append(
        {"type": "HEARTBEAT", "base_mode": 1, "custom_mode": PX4_AUTO_MISSION_CUSTOM_MODE}
    )
    lifecycle.start(object(), object(), SimpleNamespace(mission_replaced=True))

    assert lifecycle.mode_events == ["AUTO.MISSION", "AUTO.MISSION"]
    assert lifecycle.command_events.count("MAV_CMD_COMPONENT_ARM_DISARM:arm") == 1
    assert "MAV_CMD_COMPONENT_ARM_DISARM:arm_retry_2" not in lifecycle.command_events


@pytest.mark.parametrize(
    ("response", "mode", "custom_mode"),
    [("hold", "POSCTL", 3 << 16), ("land", "LAND", (4 << 16) | (6 << 24))],
)
def test_safety_mode_is_confirmed_before_logical_mission_clear(response, mode, custom_mode):
    connection = _FakeConnection()
    connection.heartbeats = [{"type": "HEARTBEAT", "base_mode": 129, "custom_mode": custom_mode}]
    lifecycle = DisposableSitlAutonomousLifecycle(
        connection, mavutil_module=_FakeMavutil, clock=lambda: 0.0, timeout_s=1.0
    )
    decision = SimpleNamespace(response=SimpleNamespace(value=response), model_proposal=None)
    probe = ExternalOnboardDockerProbe(
        "udpin:127.0.0.1:24561",
        model_path=__file__,
        model_sha256="b" * 64,
        airframe_hash="c" * 64,
    )
    probe.autonomous_lifecycle = lifecycle
    context = _context(
        _probe(),
        captured_at_s=12.5,
        airframe_profile_hash="c" * 64,
        home=GeoPoint(1.3521, 103.8198),
    )
    assert context is not None
    with pytest.raises(RuntimeError, match="confirmed PX4 safety mode"):
        probe._record_logical_mission_clear(context)
    probe._dispatch_safety(decision)
    probe._install_mission(None)
    probe._record_logical_mission_clear(context)

    assert lifecycle.mode_events == [mode]
    assert lifecycle.safety_mode_confirmed is True
    assert probe.logical_mission_clear_count == 1
    assert probe.installed_missions == []
    assert probe.mission_transactions == []
    assert probe.physical_mission_clear_sent is False


def test_model_return_and_sortie_return_share_one_confirmed_rtl_request():
    connection = _FakeConnection()
    lifecycle = DisposableSitlAutonomousLifecycle(
        connection, mavutil_module=_FakeMavutil, clock=lambda: 0.0, timeout_s=1.0
    )
    lifecycle.start(object(), object(), object())
    lifecycle.dispatch_safety(
        SimpleNamespace(
            response=SimpleNamespace(value="return"),
            model_proposal=SimpleNamespace(
                task_action=SimpleNamespace(value="record_location"),
                flight_plan=SimpleNamespace(action=SimpleNamespace(value="return_home")),
            ),
        )
    )
    lifecycle.request_return(object(), object())
    assert lifecycle.mode_events == ["AUTO.MISSION", "RTL"]
    assert lifecycle.safety_mode_confirmed is True


def _probe(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "position": {
            "latitude_deg": 1.3521,
            "longitude_deg": 103.8198,
            "relative_altitude_m": 35.0,
        },
        "gps": {"fix_type": 3, "eph_cm": 120, "lat": 13_521_000, "lon": 1_038_198_000},
        "battery_remaining_pct": 72.0,
        "ground_speed_mps": 6.5,
        "airspeed_mps": 7.0,
        "nominal_flight_time_s": NOMINAL_FLIGHT_TIME_S,
        "geofence_valid_declared": True,
        "camera_available_declared": True,
        "perception_available_declared": True,
        "map_cache_available_declared": True,
        "dem_available_declared": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_report_requires_home_telemetry_and_never_changes_flight_mode():
    connection = _FakeConnection()
    lifecycle = DisposableSitlAutonomousLifecycle(
        connection, mavutil_module=_FakeMavutil, clock=lambda: 0.0, timeout_s=1.0
    )
    lifecycle.start(object(), object(), object())
    lifecycle.request_return(object(), object())
    decision = SimpleNamespace(
        response=SimpleNamespace(value="hold"),
        model_proposal=SimpleNamespace(task_action=SimpleNamespace(value="report")),
    )
    with pytest.raises(RuntimeError, match="telemetry-confirmed"):
        lifecycle.dispatch_safety(decision)
    before = list(connection.mav.commands)
    lifecycle.home_arrival_confirmed = True
    lifecycle.dispatch_safety(decision)
    assert connection.mav.commands == before
    assert lifecycle.mode_events == ["AUTO.MISSION", "RTL"]
    assert "REPORT:record_only_no_mode_change" in lifecycle.safety_mode_events


def test_external_context_uses_live_battery_and_speed_and_scales_endurance() -> None:
    context = _context(
        _probe(),
        captured_at_s=12.5,
        airframe_profile_hash="a" * 64,
        home=GeoPoint(1.3521, 103.8198),
    )

    assert context is not None
    assert context.battery_remaining_pct == 72.0
    assert context.ground_speed_mps == 6.5
    assert context.airspeed_mps == 7.0
    assert context.estimated_flight_time_s == NOMINAL_FLIGHT_TIME_S * 0.72


def test_external_context_fails_closed_without_live_power_or_speed() -> None:
    probe = _probe(battery_remaining_pct=None)
    assert (
        _context(
            probe,
            captured_at_s=12.5,
            airframe_profile_hash="a" * 64,
            home=GeoPoint(1.3521, 103.8198),
        )
        is None
    )


def test_external_probe_records_sys_status_and_vfr_hud() -> None:
    probe = ExternalOnboardDockerProbe(
        "udpin:127.0.0.1:24561",
        model_path=__file__,
        model_sha256="b" * 64,
        airframe_hash="c" * 64,
    )
    probe._record({"type": "SYS_STATUS", "battery_remaining": 64})
    probe._record({"type": "VFR_HUD", "groundspeed": 4.25, "airspeed": 4.5})

    assert probe.battery_remaining_pct == 64.0
    assert probe.ground_speed_mps == 4.25
    assert probe.airspeed_mps == 4.5


def test_external_probe_uses_global_position_velocity_proxy_for_multirotor() -> None:
    probe = ExternalOnboardDockerProbe(
        "udpin:127.0.0.1:24561",
        model_path=__file__,
        model_sha256="b" * 64,
        airframe_hash="c" * 64,
    )
    probe._record(
        {
            "type": "GLOBAL_POSITION_INT",
            "lat": 13_521_000,
            "lon": 1_038_198_000,
            "relative_alt": 35_000,
            "hdg": 0,
            "vx": 300,
            "vy": 400,
        }
    )

    assert probe.ground_speed_mps == 5.0
    assert probe.airspeed_mps == 5.0
    assert probe.airspeed_source == "multirotor_groundspeed_proxy_no_pitot"


def test_external_probe_opens_sensor_capture_only_after_armed_altitude() -> None:
    gate = threading.Event()
    probe = ExternalOnboardDockerProbe(
        "udpin:127.0.0.1:24561",
        model_path=__file__,
        model_sha256="b" * 64,
        airframe_hash="c" * 64,
        execute_autonomous_flight=True,
        sensor_capture_start_event=gate,
    )
    probe.autonomous_lifecycle = SimpleNamespace(started=True)
    probe._record(
        {
            "type": "GLOBAL_POSITION_INT",
            "lat": 13_521_000,
            "lon": 1_038_198_000,
            "relative_alt": 9_999,
            "hdg": 0,
            "vx": 0,
            "vy": 0,
        }
    )
    assert gate.is_set() is False

    probe._record(
        {
            "type": "GLOBAL_POSITION_INT",
            "lat": 13_521_000,
            "lon": 1_038_198_000,
            "relative_alt": 10_000,
            "hdg": 0,
            "vx": 0,
            "vy": 0,
        }
    )
    assert gate.is_set() is True


def test_external_probe_does_not_consume_camera_frame_before_takeoff_gate() -> None:
    probe = ExternalOnboardDockerProbe(
        "udpin:127.0.0.1:24561",
        model_path=__file__,
        model_sha256="b" * 64,
        airframe_hash="c" * 64,
    )
    probe.home = GeoPoint(1.3521, 103.8198)
    probe.position = {
        "latitude_deg": 1.3521,
        "longitude_deg": 103.8198,
        "relative_altitude_m": 0.0,
    }
    probe.gps = {
        "fix_type": 3,
        "eph_cm": 120,
        "lat": 13_521_000,
        "lon": 1_038_198_000,
    }
    probe.battery_remaining_pct = 72.0
    probe.ground_speed_mps = 0.0
    probe.airspeed_mps = 0.0
    probe.bridge = SimpleNamespace(
        event_loop=SimpleNamespace(
            adapter=SimpleNamespace(task_state=SimpleNamespace(phase=OnboardTaskAction.TAKEOFF))
        )
    )
    calls: list[object] = []
    probe.frame_perception_adapter = SimpleNamespace(observe=lambda **kwargs: calls.append(kwargs))
    probe.sensor_frames = [
        (
            SimpleNamespace(image_bgr=object()),
            SimpleNamespace(frame_id="frame-001", payload_sha256="d" * 64),
            None,
        )
    ]

    probe._submit_actual_frame(1.0)

    assert probe.observation_index == 0
    assert calls == []
    assert probe.takeoff_completion_observed is False
