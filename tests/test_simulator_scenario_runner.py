from __future__ import annotations

import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from multidetect.autonomy.contracts import (
    ExecutableMission,
    MissionCommand,
    MissionFrame,
    MissionItem,
)
from multidetect.mission_progress import MissionProgressConfig, MissionProgressPhase
from multidetect.runtime import RuntimeMode
from multidetect.simulation import FaultEvent, FaultKind, load_runtime_scenario
from multidetect.simulator import (
    FlightMode,
    SimulatorScenarioRunner,
    SimulatorScenarioRunnerConfig,
    SimulatorScenarioRunnerError,
)
from multidetect.simulator.scenario_runner import main as scenario_runner_main

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "configs/scenarios/river_rescue_vtol_001.scenario.json"


def _single_item_mission() -> ExecutableMission:
    return ExecutableMission(
        mission_id="scenario-runner-progress-fixture",
        platform_plan_hash="a" * 64,
        airframe_profile_hash="b" * 64,
        airframe_adapter_id="scenario-runner-fixture-v1",
        airframe_adapter_hash="c" * 64,
        items=(
            MissionItem(
                sequence=0,
                command=MissionCommand.TAKEOFF,
                frame=MissionFrame.GLOBAL_RELATIVE_ALT_INT,
                latitude_deg=1.3,
                longitude_deg=103.8005,
                altitude_m=10.0,
            ),
        ),
    )


def test_manifest_runner_uses_runtime_channel_and_is_deterministic() -> None:
    config = SimulatorScenarioRunnerConfig(frames=8, dt_s=0.1)
    first = SimulatorScenarioRunner(MANIFEST, config=config).run()
    second = SimulatorScenarioRunner(MANIFEST, config=config).run()

    assert first.scenario_id == "river_rescue_vtol_001"
    assert first.mode == FlightMode.VTOL.value
    assert first.completed_frames == 8
    assert first.terminal_reason == "frame_budget_complete"
    assert first.closed is True
    assert first.to_mapping()["truth_loaded"] is False
    assert first.evidence_hash == second.evidence_hash
    assert first.evidence["truth_free"] is True


def test_manifest_runner_can_bind_and_start_compiled_mission_progress() -> None:
    mission = _single_item_mission()
    runner = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=1, enable_ai=False),
        mission=mission,
        mission_progress_config=MissionProgressConfig(waypoint_radius_m=10.0),
    )
    assert runner.mission is mission
    assert runner.mission_progress is None
    assert runner.start_mission() is True
    result = runner.run()
    assert result.steps[0]["mission_progress"]["mission_id"] == mission.mission_id
    assert result.steps[0]["mission_progress"]["phase"] == MissionProgressPhase.COMPLETE.value
    assert result.evidence["mission_progress"]["mission_hash"] == mission.canonical_hash
    assert runner.mission_progress is not None
    assert runner.mission_progress.phase is MissionProgressPhase.COMPLETE


def test_manifest_runner_rejects_progress_config_without_mission() -> None:
    with pytest.raises(TypeError, match="requires a mission"):
        SimulatorScenarioRunner(
            MANIFEST,
            config=SimulatorScenarioRunnerConfig(frames=1, enable_ai=False),
            mission_progress_config=MissionProgressConfig(),
        )


def test_scenario_runner_cli_binds_and_starts_mission_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mission_path = tmp_path / "mission.json"
    mission_path.write_text(
        json.dumps(_single_item_mission().to_mapping()), encoding="utf-8", newline="\n"
    )
    code = scenario_runner_main(
        [
            "--scenario",
            str(MANIFEST),
            "--mission",
            str(mission_path),
            "--start-mission",
            "--frames",
            "1",
            "--no-ai",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["steps"][0]["mission_progress"]["phase"] == (MissionProgressPhase.COMPLETE.value)
    assert payload["evidence"]["mission_progress"]["mission_id"] == (
        "scenario-runner-progress-fixture"
    )


def test_manifest_runner_binds_runtime_descriptor_and_shared_clock() -> None:
    runner = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=2, dt_s=0.25),
    )
    assert runner.clock is runner.sim.clock
    assert runner.descriptor.mode is RuntimeMode.SIM
    assert runner.descriptor.deterministic is True
    assert runner.descriptor.scenario_id == "river_rescue_vtol_001"
    assert runner.descriptor.scenario_manifest_hash == runner.runtime.scenario_manifest_hash
    result = runner.run()
    assert result.runtime_descriptor is runner.descriptor
    assert result.runtime_descriptor_hash == runner.descriptor.canonical_hash
    assert result.evidence["runtime_descriptor_hash"] == runner.descriptor.canonical_hash
    assert result.evidence["clock"]["monotonic_s"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("name", "latitude", "longitude", "elevation"),
    (
        ("river_rescue_vtol_001", 1.300000, 103.800500, 5.0),
        ("coastal_patrol_fixed_wing_001", 1.290500, 103.850500, 8.0),
    ),
)
def test_manifest_runner_aligns_fallback_with_gazebo_wgs84_origin(
    name: str,
    latitude: float,
    longitude: float,
    elevation: float,
) -> None:
    """The deterministic fallback must start in the same frame as the 3-D world."""

    manifest = ROOT / "configs/scenarios" / f"{name}.scenario.json"
    runner = SimulatorScenarioRunner(
        manifest,
        config=SimulatorScenarioRunnerConfig(frames=1, enable_ai=False),
    )
    assert runner.sim.model.config.initial_latitude_deg == pytest.approx(latitude)
    assert runner.sim.model.config.initial_longitude_deg == pytest.approx(longitude)

    result = runner.run()
    origin = result.evidence["world_origin"]
    assert origin["source"] == "gazebo_world_sdf"
    assert origin["resolved"] is True
    assert origin["latitude_deg"] == pytest.approx(latitude)
    assert origin["longitude_deg"] == pytest.approx(longitude)
    assert origin["elevation_m"] == pytest.approx(elevation)
    # The first physics step remains near the declared origin rather than
    # silently starting at Null Island.
    telemetry = result.steps[0]["telemetry_after"]
    assert telemetry["latitude_deg"] == pytest.approx(latitude, abs=1e-3)
    assert telemetry["longitude_deg"] == pytest.approx(longitude, abs=1e-3)


def test_manifest_runner_serializes_world_path_relative_to_repository() -> None:
    result = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=1, enable_ai=False),
    ).run()

    origin = result.evidence["world_origin"]
    assert origin["world_path"] == ("simulation/gazebo/worlds/river_rescue_vtol_001.sdf")
    assert not Path(origin["world_path"]).is_absolute()


def test_manifest_runner_hash_survives_equivalent_checkout_root(tmp_path: Path) -> None:
    """Moving the scenario and world assets must not alter canonical evidence."""

    migrated = tmp_path / "migrated-checkout"
    shutil.copytree(ROOT / "configs" / "scenarios", migrated / "configs" / "scenarios")
    shutil.copytree(
        ROOT / "simulation" / "gazebo" / "worlds",
        migrated / "simulation" / "gazebo" / "worlds",
    )
    migrated_manifest = migrated / "configs" / "scenarios" / MANIFEST.name
    config = SimulatorScenarioRunnerConfig(frames=3, enable_ai=False)
    original = SimulatorScenarioRunner(MANIFEST, config=config).run()
    moved = SimulatorScenarioRunner(migrated_manifest, config=config).run()

    assert moved.evidence_hash == original.evidence_hash
    assert moved.evidence["world_origin"]["world_path"] == (
        "simulation/gazebo/worlds/river_rescue_vtol_001.sdf"
    )


def test_manifest_runner_can_intentionally_opt_out_of_world_origin() -> None:
    runner = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(
            frames=1,
            enable_ai=False,
            use_world_origin=False,
        ),
    )
    assert runner.sim.model.config.initial_latitude_deg == pytest.approx(0.0)
    assert runner.sim.model.config.initial_longitude_deg == pytest.approx(0.0)
    result = runner.run()
    assert result.evidence["world_origin"]["source"] == "runner_config_opt_out"
    assert result.evidence["world_origin"]["resolved"] is False


def test_manual_provider_uses_explicit_arm_lifecycle_when_ai_is_disabled() -> None:
    """Manual-only SIM runs must advance the same armed flight model as AI runs."""

    def provider(index: int, telemetry):
        del index, telemetry
        return {"pitch": 0.2, "throttle": 0.7}

    result = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=2, enable_ai=False),
        manual_provider=provider,
    ).run()

    assert result.evidence["flight_model"]["state"]["armed"] is True
    assert result.evidence["flight_model"]["commands"][0]["name"] == "ARM"
    # Two pilot frames are followed by the adapter's single shutdown neutral;
    # closing a manual stream must not leave the last setpoint active.
    assert result.evidence["transport"]["messages"] == 3
    assert result.evidence["control"]["source_counts"] == {"manual": 2, "shutdown": 1}
    assert result.evidence["control"]["manual_control_active"] is False
    assert result.steps[-1]["telemetry_after"]["ground_speed_mps"] > 0.0


def test_manual_provider_can_explicitly_exercise_disarmed_gate() -> None:
    def provider(index: int, telemetry):
        del index, telemetry
        return {"pitch": 0.2, "throttle": 0.7}

    result = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(
            frames=2,
            enable_ai=False,
            arm_on_start=False,
        ),
        manual_provider=provider,
    ).run()

    assert result.evidence["flight_model"]["state"]["armed"] is False
    assert result.evidence["flight_model"]["commands"] == []
    assert all(step["telemetry_after"]["ground_speed_mps"] == 0.0 for step in result.steps)


def test_manifest_runner_explicit_coordinates_override_world_origin() -> None:
    runner = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(
            frames=1,
            enable_ai=False,
            initial_latitude_deg=31.2,
            initial_longitude_deg=121.5,
        ),
    )
    assert runner.sim.model.config.initial_latitude_deg == pytest.approx(31.2)
    assert runner.sim.model.config.initial_longitude_deg == pytest.approx(121.5)
    result = runner.run()
    assert result.evidence["world_origin"]["source"] == "runner_config_override"
    assert result.evidence["world_origin"]["resolved"] is False


def test_observation_provider_is_the_only_target_observation_source() -> None:
    seen: list[float] = []

    def provider(telemetry):
        seen.append(telemetry.observed_at_s)
        return replace(
            telemetry,
            target_visible=True,
            target_bearing_deg=-12.0,
            target_distance_m=42.0,
            target_confidence=0.9,
        )

    result = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=3),
        observation_provider=provider,
    ).run()
    assert seen == [0.0, 0.1, 0.2]
    decisions = [item["decision"] for item in result.steps]
    assert all(item["mode"] == "track" for item in decisions if item is not None)


def test_gps_dropout_fault_fails_closed_for_return_request() -> None:
    fault = FaultEvent(
        event_id="gps-dropout-now",
        at_s=0.0,
        kind=FaultKind.GPS_DROPOUT,
        duration_s=0.2,
    )
    runtime_input = replace(load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM), faults=(fault,))

    def provider(telemetry):
        return replace(
            telemetry,
            return_requested=True,
            home_bearing_deg=0.0,
            home_distance_m=100.0,
        )

    runner = SimulatorScenarioRunner(
        runtime_input,
        config=SimulatorScenarioRunnerConfig(frames=1),
        observation_provider=provider,
    )
    result = runner.run()
    decision = result.steps[0]["decision"]
    assert decision is not None
    assert decision["mode"] == "failsafe"
    assert "gps_unqualified_for_return" in decision["safety_flags"]
    assert result.steps[0]["observation"]["gps_fix_type"] == 0
    assert result.steps[0]["observation"]["gps_observed_at_s"] is None


def test_timestamp_skew_fault_keeps_shared_clock_and_rejects_future_gps() -> None:
    """A future sensor cursor must fail the same return-GPS freshness gate."""

    fault = FaultEvent(
        event_id="gps-clock-future",
        at_s=0.0,
        kind=FaultKind.TIMESTAMP_SKEW,
        magnitude=2.0,
    )
    runtime_input = replace(load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM), faults=(fault,))

    def provider(telemetry):
        return replace(
            telemetry,
            return_requested=True,
            home_bearing_deg=0.0,
            home_distance_m=100.0,
        )

    result = SimulatorScenarioRunner(
        runtime_input,
        config=SimulatorScenarioRunnerConfig(frames=1),
        observation_provider=provider,
    ).run()
    observation = result.steps[0]["observation"]
    decision = result.steps[0]["decision"]
    assert observation["observed_at_s"] == pytest.approx(0.0)
    assert observation["gps_observed_at_s"] == pytest.approx(2.0)
    assert decision is not None
    assert decision["mode"] == "failsafe"
    assert "gps_unqualified_for_return" in decision["safety_flags"]


def test_timestamp_skew_before_epoch_becomes_unavailable_not_invalid() -> None:
    fault = FaultEvent(
        event_id="gps-clock-stale",
        at_s=0.0,
        kind=FaultKind.TIMESTAMP_SKEW,
        magnitude=-2.0,
    )
    runtime_input = replace(load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM), faults=(fault,))
    result = SimulatorScenarioRunner(
        runtime_input,
        config=SimulatorScenarioRunnerConfig(frames=1),
    ).run()
    assert result.terminal_reason == "frame_budget_complete"
    assert result.steps[0]["observation"]["gps_observed_at_s"] is None


def test_target_drift_faults_are_deterministic_and_wrap_relative_bearing() -> None:
    faults = (
        FaultEvent(
            event_id="target-range-drift",
            at_s=0.0,
            kind=FaultKind.TARGET_DRIFT,
            magnitude=10.0,
        ),
        FaultEvent(
            event_id="target-bearing-drift",
            at_s=0.0,
            kind=FaultKind.TARGET_BEARING_DRIFT,
            magnitude=90.0,
        ),
    )
    runtime_input = replace(load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM), faults=faults)

    def provider(telemetry):
        return replace(
            telemetry,
            target_visible=True,
            target_bearing_deg=170.0,
            target_distance_m=42.0,
            target_confidence=0.9,
        )

    result = SimulatorScenarioRunner(
        runtime_input,
        config=SimulatorScenarioRunnerConfig(frames=3, dt_s=0.1),
        observation_provider=provider,
    ).run()
    observations = [step["observation"] for step in result.steps]
    assert [item["target_distance_m"] for item in observations] == pytest.approx([42.0, 43.0, 44.0])
    assert [item["target_bearing_deg"] for item in observations] == pytest.approx(
        [170.0, 179.0, -172.0]
    )
    assert result.fault_events[0]["active_frames"] == 3
    assert result.fault_events[1]["active_frames"] == 3
    assert result.fault_events[0]["fired"] is True
    assert result.fault_events[1]["fired"] is True


def test_fault_without_duration_remains_active_and_battery_step_persists() -> None:
    fault = FaultEvent(
        event_id="battery-step-now",
        at_s=0.0,
        kind=FaultKind.BATTERY_STEP_DROP,
        magnitude=20.0,
    )
    runtime_input = replace(load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM), faults=(fault,))
    result = SimulatorScenarioRunner(
        runtime_input,
        config=SimulatorScenarioRunnerConfig(frames=3),
    ).run()
    batteries = [item["observation"]["battery_remaining_pct"] for item in result.steps]
    assert batteries == [80.0, 80.0, 80.0]
    assert result.fault_events[0]["active_frames"] == 3
    assert result.fault_events[0]["fired"] is True


def test_finite_fault_sets_fired_when_its_window_is_observed() -> None:
    fault = FaultEvent(
        event_id="finite-camera-dropout",
        at_s=0.1,
        kind=FaultKind.CAMERA_DROPOUT,
        duration_s=0.2,
    )
    runtime_input = replace(load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM), faults=(fault,))
    result = SimulatorScenarioRunner(
        runtime_input,
        config=SimulatorScenarioRunnerConfig(frames=4, dt_s=0.1),
    ).run()
    assert result.fault_events[0]["active_frames"] == 2
    assert result.fault_events[0]["fired"] is True


def test_fixed_wing_mode_can_be_selected_without_loading_truth() -> None:
    runner = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=2, mode="fixed-wing"),
    )
    result = runner.run()
    assert result.mode == FlightMode.FIXED_WING.value
    assert result.evidence["flight_model"]["mode"] == "fixed-wing"


def test_multirotor_manifest_infers_mode_and_keeps_truth_out_of_runtime() -> None:
    manifest = ROOT / "configs/scenarios/urban_inspection_multirotor_001.scenario.json"
    runner = SimulatorScenarioRunner(
        manifest,
        config=SimulatorScenarioRunnerConfig(frames=2),
    )
    result = runner.run()
    assert result.mode == FlightMode.MULTIROTOR.value
    assert result.evidence["flight_model"]["mode"] == "multirotor"
    assert result.to_mapping()["truth_loaded"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"frames": 0},
        {"frames": True},
        {"dt_s": 0.0},
        {"dt_s": float("nan")},
        {"active_fault_ids": ("missing",)},
    ],
)
def test_runner_rejects_invalid_configuration(kwargs: dict[str, object]) -> None:
    if "active_fault_ids" in kwargs:
        with pytest.raises(SimulatorScenarioRunnerError):
            SimulatorScenarioRunner(MANIFEST, config=SimulatorScenarioRunnerConfig(**kwargs))
    else:
        with pytest.raises(ValueError):
            SimulatorScenarioRunnerConfig(**kwargs)


@pytest.mark.parametrize("latitude_deg", (-90.0, 90.0))
def test_runner_rejects_exact_geographic_poles_before_model_construction(
    latitude_deg: float,
) -> None:
    with pytest.raises(ValueError, match=r"within \(-90, 90\)"):
        SimulatorScenarioRunnerConfig(initial_latitude_deg=latitude_deg)
