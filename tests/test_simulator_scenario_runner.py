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
from multidetect.autonomy.policy_artifact import PolicyArtifact
from multidetect.autonomy.policy_contract import SharedTaskPolicy
from multidetect.autonomy.serialization import canonical_sha256
from multidetect.autonomy.task_ranker import TaskRanker
from multidetect.mission_progress import MissionProgressConfig, MissionProgressPhase
from multidetect.runtime import RuntimeMode, load_runtime_run_report
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


def test_energy_profile_loads_from_hash_bound_runtime_channel(tmp_path: Path) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for channel in manifest["channels"].values():
        shutil.copyfile(MANIFEST.parent / channel["path"], tmp_path / channel["path"])
    runtime_path = tmp_path / manifest["channels"]["runtime_input"]["path"]
    raw = json.loads(runtime_path.read_text(encoding="utf-8"))
    raw["vehicle"]["energy_profile"] = {
        "capacity_wh": 10,
        "avionics_power_w": 36,
        "propulsion_max_power_w": 324,
        "provenance": "integration fixture only; not physically calibrated",
    }
    runtime_path.write_text(json.dumps(raw), encoding="utf-8")
    manifest["channels"]["runtime_input"]["sha256"] = canonical_sha256(raw)
    manifest_path = tmp_path / MANIFEST.name
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    config = SimulatorScenarioRunnerConfig(
        frames=2,
        dt_s=10,
        enable_ai=False,
        arm_on_start=False,
        use_world_origin=False,
    )
    runner = SimulatorScenarioRunner(manifest_path, config=config)
    profile = runner.runtime_input.vehicle.energy_profile
    assert profile.capacity_wh == 10
    baseline = SimulatorScenarioRunner(MANIFEST, config=config)
    assert (
        runner.runtime_descriptor.airframe_profile_hash
        != baseline.runtime_descriptor.airframe_profile_hash
    )
    result = runner.run()
    assert result.completed_frames == 2
    assert runner.sim.model.battery_remaining_pct == pytest.approx(98)
    assert runner.sim.model.energy_observation()["provenance"] == profile.provenance
    assert '"energy"' in json.dumps(result.evidence)
    # Identical on-disk input and simulation time reproduce the exact output.
    assert (
        SimulatorScenarioRunner(manifest_path, config=config).run().evidence_hash
        == result.evidence_hash
    )
    raw["vehicle"]["energy_profile"]["capacity_wh"] = 20
    runtime_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SimulatorScenarioRunnerError):
        SimulatorScenarioRunner(manifest_path, config=config)  # stale channel hash


def _shared_policy_for_runner(runner: SimulatorScenarioRunner) -> SharedTaskPolicy:
    ranker = TaskRanker((0.0, 0.0, 0.0), "simulator-runner-fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="simulator-runner-fixture",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=runner.descriptor.airframe_profile_hash,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    return SharedTaskPolicy(ranker, artifact)


def test_shared_policy_identity_is_bound_without_mixing_actuator_policy() -> None:
    runner = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=2),
    )
    policy = _shared_policy_for_runner(runner)
    runner = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=2),
        shared_policy=policy,
        expected_policy_artifact_sha256=policy.artifact_sha256,
    )
    result = runner.run()
    binding = result.evidence["shared_task_policy"]
    assert binding["status"] == "identity_bound_only"
    assert binding["policy_artifact_sha256"] == policy.artifact_sha256
    assert binding["planner_revalidation_required"] is True
    assert binding["planner_revalidated"] is False
    assert binding["policy_applied_to_actuator"] is False
    assert binding["low_level_actuator_policy_separate"] is True


def test_shared_policy_runner_fails_closed_on_artifact_or_airframe_mismatch() -> None:
    baseline = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=1),
    )
    policy = _shared_policy_for_runner(baseline)
    with pytest.raises(SimulatorScenarioRunnerError, match="expected identity"):
        SimulatorScenarioRunner(
            MANIFEST,
            config=SimulatorScenarioRunnerConfig(frames=1),
            shared_policy=policy,
            expected_policy_artifact_sha256="3" * 64,
        )
    foreign_ranker = TaskRanker((0.0, 0.0, 0.0), "foreign-airframe-fixture")
    foreign_artifact = PolicyArtifact.for_ranker(
        foreign_ranker,
        policy_id="foreign-airframe-fixture",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash="4" * 64,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    with pytest.raises(SimulatorScenarioRunnerError, match="airframe profile"):
        SimulatorScenarioRunner(
            MANIFEST,
            config=SimulatorScenarioRunnerConfig(frames=1),
            shared_policy=SharedTaskPolicy(foreign_ranker, foreign_artifact),
        )


def test_shared_policy_selects_revalidates_compiles_and_binds_mission() -> None:
    from scripts.train_task_ranker import load_dataset

    groups, _, _ = load_dataset(ROOT / "configs/training/task-ranker-budget-v1.json")
    case = groups["train"][0]
    base = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=2, enable_ai=False),
    )
    ranker = TaskRanker((0.0, 0.0, 0.0), "task-binding-fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-binding-fixture",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=base.descriptor.airframe_profile_hash,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    policy = SharedTaskPolicy(ranker, artifact)
    runner = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=2, enable_ai=False),
        shared_policy=policy,
        expected_policy_artifact_sha256=artifact.canonical_hash,
    )
    context = replace(
        case.context,
        airframe_profile_hash=base.descriptor.airframe_profile_hash,
    )
    binding = runner.select_and_bind_task(
        case.candidates,
        context,
        case.vehicle_class,
        runtimes=case.runtimes,
    )
    assert binding.decision.action.value == "select"
    assert binding.decision.planner_revalidated is True
    assert binding.mission_bound is True
    assert binding.mission_started is False
    assert runner.mission is not None
    result = runner.run()
    task_binding = result.evidence["shared_task_policy"]["task_binding"]
    assert task_binding["executable_mission_sha256"] == binding.executable_mission_sha256
    assert task_binding["policy_applied_to_actuator"] is False
    runtime_binding = result.evidence["shared_task_policy"]["runtime_binding"]
    assert runtime_binding["policy_artifact_sha256"] == artifact.canonical_hash
    assert runtime_binding["ranker_sha256"] == ranker.canonical_hash
    assert runtime_binding["planner_revalidated"] is True
    assert runtime_binding["mission_bound"] is True
    assert runtime_binding["mission_started"] is False
    assert runtime_binding["policy_applied_to_actuator"] is False


def test_shared_policy_abstention_is_recorded_without_binding_mission() -> None:
    from scripts.train_task_ranker import load_dataset

    groups, _, _ = load_dataset(ROOT / "configs/training/task-ranker-budget-v1.json")
    case = groups["train"][0]
    base = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=1, enable_ai=False),
    )
    context = replace(
        case.context,
        gps_fix_type=0,
        airframe_profile_hash=base.descriptor.airframe_profile_hash,
    )
    ranker = TaskRanker((0.0, 0.0, 0.0), "task-abstain-fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-abstain-fixture",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=base.descriptor.airframe_profile_hash,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    runner = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=1, enable_ai=False),
        shared_policy=SharedTaskPolicy(ranker, artifact),
    )
    binding = runner.select_and_bind_task(
        case.candidates,
        context,
        case.vehicle_class,
        runtimes=case.runtimes,
    )
    assert binding.decision.action.value == "abstain"
    assert binding.selected_task_id is None
    assert binding.mission_bound is False
    assert runner.mission is None
    runtime_binding = runner.task_policy_runtime_binding
    assert runtime_binding is not None
    assert runtime_binding.action == "abstain"
    assert runtime_binding.selected_task_id is None
    assert runtime_binding.mission_bound is False
    assert runtime_binding.reason == binding.decision.reason


def test_shared_policy_rejects_context_from_another_airframe() -> None:
    from scripts.train_task_ranker import load_dataset

    groups, _, _ = load_dataset(ROOT / "configs/training/task-ranker-budget-v1.json")
    case = groups["train"][0]
    base = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=1, enable_ai=False),
    )
    ranker = TaskRanker((0.0, 0.0, 0.0), "task-context-identity-fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-context-identity-fixture",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=base.descriptor.airframe_profile_hash,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    runner = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=1, enable_ai=False),
        shared_policy=SharedTaskPolicy(ranker, artifact),
    )
    with pytest.raises(SimulatorScenarioRunnerError, match="context airframe profile"):
        runner.select_and_bind_task(
            case.candidates,
            replace(case.context, airframe_profile_id="foreign-airframe"),
            case.vehicle_class,
            runtimes=case.runtimes,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"capacity_wh": 0},
        {"capacity_wh": True},
        {"capacity_wh": float("nan")},
        {"avionics_power_w": -1},
        {"avionics_power_w": 0, "propulsion_max_power_w": 0},
        {"provenance": " "},
        {"target_truth": 12},
    ],
)
def test_runtime_energy_profile_rejects_bad_configuration(changes) -> None:
    from multidetect.simulation.runtime_input import VehicleRuntimeInput

    raw = load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM).vehicle.to_mapping()
    raw["energy_profile"] = {
        "capacity_wh": 10,
        "avionics_power_w": 36,
        "propulsion_max_power_w": 324,
        "provenance": "fixture",
        **changes,
    }
    with pytest.raises(ValueError):
        VehicleRuntimeInput.from_mapping(raw)


def test_energy_battery_fault_offsets_live_state_without_recharging_it() -> None:
    from multidetect.simulation.runtime_input import VehicleEnergyProfile
    from multidetect.simulator.flight_model import FlightModelConfig

    runtime = load_runtime_scenario(MANIFEST, mode=RuntimeMode.SIM)
    runtime = replace(
        runtime,
        vehicle=replace(
            runtime.vehicle, energy_profile=VehicleEnergyProfile(10, 360, 0, "fixture")
        ),
        faults=(
            FaultEvent(
                event_id="energy-drop",
                at_s=0,
                kind=FaultKind.BATTERY_STEP_DROP,
                magnitude=10,
                duration_s=15,
            ),
        ),
    )
    runner = SimulatorScenarioRunner(
        runtime,
        config=SimulatorScenarioRunnerConfig(enable_ai=False, arm_on_start=False),
        model_config=FlightModelConfig(initial_battery_remaining_pct=20),
    )
    model = runner.sim.model
    assert runner._observation(model.telemetry).battery_remaining_pct == 10
    model.step({}, 10)  # Background energy falls to 10%, not a fixed 100% baseline.
    assert runner._observation(model.telemetry).battery_remaining_pct == 0
    assert model.battery_remaining_pct == 10
    model.step({}, 5)  # Finite sensor fault ends; restore current 5%, not pre-fault 20%.
    assert runner._observation(model.telemetry).battery_remaining_pct == 5
    assert model.battery_remaining_pct == 5
    assert runner.sim.model.energy_observation()["remaining_wh"] == 0.5


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
    assert result.to_mapping()["runtime_input_hash"] == canonical_sha256(
        runner.runtime.to_channel_mapping()
    )
    assert result.evidence["runtime_descriptor_hash"] == runner.descriptor.canonical_hash
    assert result.evidence["clock"]["monotonic_s"] == pytest.approx(0.5)


def test_manifest_runner_exposes_hash_bound_bounded_runtime_report(tmp_path: Path) -> None:
    runner = SimulatorScenarioRunner(
        MANIFEST,
        config=SimulatorScenarioRunnerConfig(frames=3, dt_s=0.25, enable_ai=False),
    )
    result = runner.run()

    report = result.runtime_run_report
    assert report.descriptor_hash == result.runtime_descriptor_hash
    assert report.input_hash == canonical_sha256(runner.runtime.to_channel_mapping())
    assert report.processed_frame_ids == (
        "sim:river_rescue_vtol_001:0",
        "sim:river_rescue_vtol_001:1",
        "sim:river_rescue_vtol_001:2",
    )
    assert report.processed_frame_count == result.completed_frames == 3
    assert report.complete is False
    assert report.max_frames == result.requested_frames == 3
    assert report.termination_reason == "frame_limit_reached"
    assert report.finished_at_s == pytest.approx(0.75)

    destination = result.write_runtime_run_report(tmp_path / "sim-runtime-run.json")
    assert load_runtime_run_report(destination) == report


def test_manifest_runner_cli_can_persist_shared_runtime_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = tmp_path / "cli-runtime-run.json"
    code = scenario_runner_main(
        [
            "--scenario",
            str(MANIFEST),
            "--frames",
            "2",
            "--no-ai",
            "--runtime-report",
            str(destination),
        ]
    )

    assert code == 0
    json.loads(capsys.readouterr().out)
    report = load_runtime_run_report(destination)
    assert report.processed_frame_count == 2
    assert report.max_frames == 2
    assert report.complete is False


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
