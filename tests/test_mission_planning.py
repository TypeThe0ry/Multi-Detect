from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime

import pytest

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    PlanAction,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
    plan_task,
)
from multidetect.autonomy.mission_campaign import (
    CampaignTaskBinding,
    select_and_compile_task,
)
from multidetect.autonomy.task_selection import TaskCandidate
from multidetect.autonomy.task_session import run_observed_task_session
from multidetect.clock import ManualClock
from multidetect.mission_compile_cli import main as mission_compile_cli_main
from multidetect.mission_planning import (
    MISSION_COMPILE_RUNTIME_SCHEMA_VERSION,
    PlannedMissionCompilation,
    compile_planned_task_mission,
    compile_planned_task_mission_from_runtime,
    load_executable_mission_document,
    parse_mission_compile_runtime,
)
from multidetect.px4_mission import (
    FixedWingMissionGeometry,
    MultirotorMissionGeometry,
    Px4FixedWingSafetyPolicy,
    Px4MissionSafetyPolicy,
    Px4MultirotorSafetyPolicy,
    VtolMissionGeometry,
)
from multidetect.runtime import RuntimeMode
from multidetect.simulation.energy_replay import CumulativeSimulationEnergy
from multidetect.simulation.task_episode import simulate_task_budget_episode
from multidetect.simulator.flight_model import SimulationEnergyProfile


def _energy_history(end):
    samples = []
    for at in range(10, int(end) + 1):
        for kind, field, value in (
            ("HEARTBEAT", "base_mode", 128),
            ("VFR_HUD", "throttle", 50),
            ("GLOBAL_POSITION_INT", "time_boot_ms", at * 1000),
        ):
            samples.append(
                dict(
                    type=kind,
                    field=field,
                    value=value,
                    at_s=at,
                    source_system=1,
                    source_component=1,
                )
            )
    return dict(
        samples=samples,
        samples_seen=len(samples),
        samples_dropped=0,
        receive_origin_host_monotonic_s=100,
        receive_clock="host_monotonic_since_collector_start",
    )


def _energy_state():
    return CumulativeSimulationEnergy(
        SimulationEnergyProfile(1000, 36, 324, "SIM fixture only"),
        session_id="capture",
        initial_remaining_wh=1000,
    )


def test_observed_session_consumes_cumulative_energy_through_shared_context():
    context, clock, candidates, kwargs = _observed_session_inputs()
    state = _energy_state()
    energies = []

    def backend(compilation, monitor):
        _complete_observed(compilation, monitor, clock)
        observed = replace(
            context,
            snapshot_id=f"after-{len(energies)}",
            captured_at_s=clock.monotonic(),
            captured_at_utc=clock.utc_now().isoformat(),
        )
        projected, energy = state.project_context(
            observed,
            _energy_history(clock.monotonic()),
            session_id="capture",
            clock=clock,
            runtime_mode=RuntimeMode.SIM,
        )
        # Preserve original sensor fields and timestamp, not just position.
        assert (
            replace(
                projected,
                battery_remaining_pct=observed.battery_remaining_pct,
                estimated_flight_time_s=observed.estimated_flight_time_s,
            )
            == observed
        )
        energies.append(energy)
        return projected

    result = run_observed_task_session(
        candidates, context, "multirotor", observe_task=backend, **kwargs
    )
    assert result.stop_reason == "all_tasks_completed"
    assert len(energies) == 2
    assert energies[1]["remaining_wh"] < energies[0]["remaining_wh"]
    assert result.steps[1].compilation.plan.context_snapshot_hash == (
        result.steps[0].after.canonical_hash
    )


@pytest.mark.parametrize("failure", ["REAL", "HIL", "REPLAY", "stale", "utc", "future", "session"])
def test_energy_projection_rejects_bad_join_without_consuming(failure):
    context, clock, _, _ = _observed_session_inputs()
    clock.advance(2)
    context = replace(context, captured_at_s=12, captured_at_utc=clock.utc_now().isoformat())
    mode = RuntimeMode.SIM
    session = "capture"
    if failure in ("REAL", "HIL", "REPLAY"):
        mode = RuntimeMode[failure]
    elif failure == "stale":
        context = replace(context, captured_at_s=10)
    elif failure == "utc":
        context = replace(context, captured_at_utc="2026-09-04T00:00:00Z")
    elif failure == "future":
        context = replace(context, captured_at_s=11.5)
    else:
        session = "other"
    state = _energy_state()
    with pytest.raises(ValueError):
        state.project_context(
            context, _energy_history(12), session_id=session, clock=clock, runtime_mode=mode
        )
    valid = state.observe(_energy_history(12), session_id="capture")
    assert valid["newly_consumed_wh"] == valid["consumed_wh"]


def test_projection_preserves_bad_sensor_quality_and_rejects_identity_change():
    context, clock, _, _ = _observed_session_inputs()
    clock.advance(2)
    context = replace(
        context,
        captured_at_s=12,
        captured_at_utc=clock.utc_now().isoformat(),
        gps_fix_type=0,
        geofence_valid=False,
        perception_available=False,
    )
    state = _energy_state()
    projected, _ = state.project_context(
        context,
        _energy_history(12),
        session_id="capture",
        clock=clock,
        runtime_mode=RuntimeMode.SIM,
    )
    assert projected.gps_fix_type == 0
    assert not projected.geofence_valid and not projected.perception_available
    with pytest.raises(ValueError, match="identity"):
        state.project_context(
            replace(context, airframe_profile_id="other"),
            _energy_history(12),
            session_id="capture",
            clock=clock,
            runtime_mode=RuntimeMode.SIM,
        )


def test_new_heartbeat_cannot_refresh_old_energy_clock_sample():
    context, clock, _, _ = _observed_session_inputs()
    clock.advance(4)
    context = replace(context, captured_at_s=14, captured_at_utc=clock.utc_now().isoformat())
    history = _energy_history(14)
    history["samples"] = [
        s for s in history["samples"] if s["type"] != "GLOBAL_POSITION_INT" or s["at_s"] <= 11
    ]
    history["samples_seen"] = len(history["samples"])
    with pytest.raises(ValueError, match="energy sample"):
        _energy_state().project_context(
            context, history, session_id="capture", clock=clock, runtime_mode=RuntimeMode.SIM
        )


def _observed_session_inputs():
    context = _context("multirotor")
    clock = ManualClock(context.captured_at_s, datetime.fromisoformat(context.captured_at_utc))
    geometry, policy = _geometry_and_policy("multirotor")
    runtime = {"schema_version": 1, **geometry.to_mapping(), "safety_policy": policy.to_mapping()}
    candidates = [TaskCandidate(replace(_task(), task_id=f"observed-{i}")) for i in range(2)]
    kwargs = dict(
        runtimes={c.task.task_id: runtime for c in candidates},
        clock=clock,
        airframe_profile_hash=PROFILE_HASH,
        expected_system_id=1,
        expected_component_id=1,
    )
    return context, clock, candidates, kwargs


def _complete_observed(compilation, monitor, clock):
    clock.advance(2)
    terminal = len(compilation.compiled_mission.executable_mission.items) - 1
    for kind in ("MISSION_CURRENT", "MISSION_ITEM_REACHED"):
        monitor.ingest_message(
            {"message_type": kind, "seq": terminal, "source_system_id": 1, "source_component_id": 1}
        )
    monitor.ingest_message(
        {
            "message_type": "HEARTBEAT",
            "base_mode": 0,
            "source_system_id": 1,
            "source_component_id": 1,
        }
    )
    monitor.ingest_message(
        {
            "message_type": "EXTENDED_SYS_STATE",
            "landed_state": 1,
            "source_system_id": 1,
            "source_component_id": 1,
        }
    )


@pytest.mark.parametrize("use_ranker", [False, True])
def test_observed_session_optional_ranker_preserves_original_execution(use_ranker):
    from multidetect.autonomy.task_ranker import TaskRanker

    context, clock, candidates, kwargs = _observed_session_inputs()
    candidates = [TaskCandidate(item.task, index) for index, item in enumerate(candidates)]
    ranker = TaskRanker((-1, 0, 0), "untrained fixture") if use_ranker else None
    seen = []

    def backend(compilation, monitor):
        seen.append(compilation.estimate.task_id)
        _complete_observed(compilation, monitor, clock)
        return replace(context, snapshot_id=f"after-{len(seen)}",
                       captured_at_s=clock.monotonic(),
                       captured_at_utc=clock.utc_now().isoformat())

    result = run_observed_task_session(
        candidates, context, "multirotor", observe_task=backend, ranker=ranker, **kwargs
    )
    assert result.stop_reason == "all_tasks_completed"
    assert seen == (["observed-0", "observed-1"] if use_ranker else
                    ["observed-1", "observed-0"])
    assert all(step.ranker_sha256 == (ranker.canonical_hash if ranker else None)
               for step in result.steps)


def test_shared_policy_artifact_is_bound_across_budget_and_observed_paths():
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker

    context, clock, candidates, observed_kwargs = _observed_session_inputs()
    geometry, safety_policy = _geometry_and_policy("multirotor")
    runtime = {
        "schema_version": 1,
        "vehicle_class": "multirotor",
        **geometry.to_mapping(),
        "safety_policy": safety_policy.to_mapping(),
    }
    ranker = TaskRanker((-1, 0, 0), "shared-sim-observed-fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=PROFILE_HASH,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    shared = SharedTaskPolicy(ranker, artifact)
    budget = simulate_task_budget_episode(
        candidates,
        context,
        "multirotor",
        runtimes={candidate.task.task_id: runtime for candidate in candidates},
        airframe_profile_hash=PROFILE_HASH,
        shared_policy=shared,
    )

    def backend(compilation, monitor):
        _complete_observed(compilation, monitor, clock)
        return replace(
            context,
            snapshot_id=f"after-{compilation.estimate.task_id}",
            captured_at_s=clock.monotonic(),
            captured_at_utc=clock.utc_now().isoformat(),
        )

    observed = run_observed_task_session(
        candidates,
        context,
        "multirotor",
        observe_task=backend,
        shared_policy=shared,
        **observed_kwargs,
    )
    assert budget.ranker_sha256 == ranker.canonical_hash
    assert budget.policy_artifact_sha256 == artifact.canonical_hash
    assert observed.stop_reason == "all_tasks_completed"
    assert observed.steps
    assert all(step.ranker_sha256 == ranker.canonical_hash for step in observed.steps)
    assert all(step.policy_artifact_sha256 == artifact.canonical_hash for step in observed.steps)
    assert all(step.policy_binding is not None for step in observed.steps)
    assert all(
        step.policy_binding.policy_artifact_sha256 == artifact.canonical_hash
        and step.policy_binding.ranker_sha256 == ranker.canonical_hash
        and step.policy_binding.planner_revalidated
        and step.policy_binding.mission_bound
        and not step.policy_binding.mission_started
        and not step.policy_binding.policy_applied_to_actuator
        and step.policy_binding.truth_free
        for step in observed.steps
    )


def test_observed_shared_policy_rejects_airframe_profile_mismatch():
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker

    context, _, candidates, observed_kwargs = _observed_session_inputs()
    ranker = TaskRanker((-1, 0, 0), "shared-sim-observed-mismatch-fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=PROFILE_HASH,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    with pytest.raises(ValueError, match="airframe profile mismatch"):
        run_observed_task_session(
            candidates,
            context,
            "multirotor",
            observe_task=lambda *_: None,
            shared_policy=SharedTaskPolicy(ranker, artifact),
            **{**observed_kwargs, "airframe_profile_hash": "c" * 64},
        )


def test_observed_shared_policy_rejects_decision_identity_drift(monkeypatch):
    from dataclasses import replace as dataclass_replace

    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker

    context, clock, candidates, observed_kwargs = _observed_session_inputs()
    ranker = TaskRanker((-1, 0, 0), "shared-sim-observed-drift-fixture")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=PROFILE_HASH,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    original_decide = SharedTaskPolicy.decide

    def drifted_decide(self, entries, snapshot, vehicle):
        decision, selected = original_decide(self, entries, snapshot, vehicle)
        return dataclass_replace(decision, policy_artifact_sha256="0" * 64), selected

    monkeypatch.setattr(SharedTaskPolicy, "decide", drifted_decide)
    with pytest.raises(ValueError, match="shared policy decision identity mismatch"):
        run_observed_task_session(
            candidates,
            context,
            "multirotor",
            observe_task=lambda *_: None,
            shared_policy=SharedTaskPolicy(ranker, artifact),
            **observed_kwargs,
        )


def test_observed_session_consumes_new_telemetry_not_budget_estimates() -> None:
    context, clock, candidates, kwargs = _observed_session_inputs()
    observed = []

    def backend(compilation, monitor):
        _complete_observed(compilation, monitor, clock)
        after = replace(
            context,
            snapshot_id=f"after-{len(observed)}",
            captured_at_s=clock.monotonic(),
            captured_at_utc=clock.utc_now().isoformat(),
            battery_remaining_pct=80 - len(observed) * 7,
            estimated_flight_time_s=1500,
        )
        observed.append(after)
        return after

    result = run_observed_task_session(
        candidates, context, "multirotor", observe_task=backend, **kwargs
    )
    assert result.stop_reason == "all_tasks_completed"
    assert len(result.steps) == 2
    assert result.context == observed[-1]
    assert result.context.battery_remaining_pct == 73
    assert result.steps[1].compilation.plan.context_snapshot_hash == observed[0].canonical_hash
    assert result.remaining_task_ids == ()


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("early", "mission_waiting"),
        ("timeout", "mission_timeout"),
        ("missing", "missing_post_mission_snapshot"),
        ("stale", "invalid_post_mission_snapshot"),
        ("profile", "post_mission_identity_or_time_mismatch"),
    ],
)
def test_observed_session_stops_before_second_task(mode, reason) -> None:
    context, clock, candidates, kwargs = _observed_session_inputs()
    calls = []

    def backend(compilation, monitor):
        calls.append(compilation)
        if mode == "early":
            return context
        if mode == "timeout":
            clock.advance(901)
            return None
        _complete_observed(compilation, monitor, clock)
        if mode == "missing":
            return None
        if mode == "stale":
            return context
        return replace(
            context,
            snapshot_id="new",
            captured_at_s=clock.monotonic(),
            captured_at_utc=clock.utc_now().isoformat(),
            airframe_profile_id="other",
        )

    result = run_observed_task_session(
        candidates, context, "multirotor", observe_task=backend, **kwargs
    )
    assert result.stop_reason == reason
    assert len(calls) == 1
    assert len(result.remaining_task_ids) == 2


def test_observed_session_rejects_initial_stale_context_before_callback() -> None:
    context, clock, candidates, kwargs = _observed_session_inputs()
    clock.advance(2)

    def backend(*args):
        pytest.fail("must not execute")

    with pytest.raises(ValueError, match="stale"):
        run_observed_task_session(candidates, context, "multirotor", observe_task=backend, **kwargs)


@pytest.mark.parametrize("vehicle", ("vtol", "fixed-wing", "multirotor"))
def test_budget_episode_replans_with_new_energy_time_and_px4_binding(vehicle: str) -> None:
    geometry, policy = _geometry_and_policy(vehicle)
    runtime = {
        "schema_version": 1,
        "vehicle_class": vehicle,
        **geometry.to_mapping(),
        "safety_policy": policy.to_mapping(),
    }
    candidates = [TaskCandidate(replace(_task(), task_id=f"task-{i}")) for i in range(12)]
    context = _context(vehicle)
    kwargs = dict(
        runtimes={entry.task.task_id: runtime for entry in candidates},
        airframe_profile_hash=PROFILE_HASH,
    )
    episode = simulate_task_budget_episode(candidates, context, vehicle, **kwargs)
    assert episode == simulate_task_budget_episode(reversed(candidates), context, vehicle, **kwargs)
    assert 1 < len(episode.steps) < len(candidates)
    assert episode.rejected
    for i, step in enumerate(episode.steps):
        assert step.before == (context if i == 0 else episode.steps[i - 1].after)
        assert step.compilation.plan.context_snapshot_hash == step.before.canonical_hash
        assert step.compilation.compiled_mission.executable_mission.platform_plan_hash == (
            step.compilation.plan.canonical_hash
        )
        assert step.after.captured_at_s > step.before.captured_at_s
        assert step.after.captured_at_utc != step.before.captured_at_utc
        assert step.after.battery_remaining_pct < step.before.battery_remaining_pct
        assert step.after.estimated_flight_time_s < step.before.estimated_flight_time_s
        assert step.after.aircraft_position == context.home_position
        assert step.after.battery_remaining_pct >= 45.0
    assert context == _context(vehicle)


def _training_case():
    from multidetect.simulation.task_training import TaskTrainingCase

    geometry, policy = _geometry_and_policy("vtol")
    runtime = {"schema_version": 1, **geometry.to_mapping(),
               "safety_policy": policy.to_mapping()}
    candidates = tuple(TaskCandidate(replace(_task(), task_id=f"fit-{i}"), i)
                       for i in range(12))
    return TaskTrainingCase("fixture-train", _context("vtol"), "vtol", candidates,
                            {c.task.task_id: runtime for c in candidates}, PROFILE_HASH)


def test_budget_fit_uses_existing_ranker_and_improves_explicit_fixture_objective():
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.task_ranker import TaskRanker
    from multidetect.simulation.task_training import evaluate_task_ranker, fit_task_ranker

    case = _training_case()
    parent = TaskRanker((-1, 0, 0), "deliberately poor untrained fixture parent")
    def factory(model):
        return PolicyArtifact.for_ranker(
            model,
            policy_id="training-fixture",
            policy_version="0.3.0-pre.8",
            airframe_profile_hash=PROFILE_HASH,
            mission_contract_hash="1" * 64,
            safety_policy_hash="2" * 64,
        )
    winner, report = fit_task_ranker(
        [case], parent, [(0, 0, 0), (1, 0, 0)], policy_artifact_factory=factory
    )
    assert winner.coefficients == (0, 0, 0)
    assert report["parent_sha256"] == parent.canonical_hash
    assert report["evaluations"][report["selected_trial"]]["reward"] > (
        report["evaluations"][0]["reward"]
    )
    assert report["holdout_used_for_selection"] is False
    assert report["evaluations"][report["selected_trial"]]["policy_artifact_sha256"] == (
        factory(winner).canonical_hash
    )
    assert fit_task_ranker(
        [case], parent, [(0, 0, 0), (1, 0, 0)], policy_artifact_factory=factory
    ) == (winner, report)
    evaluation = evaluate_task_ranker([case], winner, policy_artifact=factory(winner))
    assert evaluation["ranker_sha256"] == winner.canonical_hash
    assert evaluation["policy_artifact_sha256"] == factory(winner).canonical_hash
    assert evaluation["dynamics_or_physical_performance_claimed"] is False
    with pytest.raises(ValueError, match="overlaps"):
        evaluate_task_ranker([replace(case, case_id="renamed-holdout")], winner,
                             forbidden_input_hashes=report["training_input_hashes"])


def test_budget_fit_retains_parent_on_tie_and_rejects_bad_search():
    from multidetect.autonomy.task_ranker import TaskRanker
    from multidetect.simulation.task_training import evaluate_task_ranker, fit_task_ranker

    case = _training_case()
    parent = TaskRanker((0, 0, 0), "baseline fixture")
    winner, report = fit_task_ranker([case], parent, [(0, 0, 0)])
    assert winner is parent and report["selected_trial"] == 0
    with pytest.raises(ValueError):
        fit_task_ranker([case], parent, [])
    with pytest.raises(ValueError):
        fit_task_ranker([case], parent, [(float("nan"), 0, 0)])
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_task_ranker([case, replace(case, case_id="different-id")], parent)
    with pytest.raises(ValueError):
        fit_task_ranker([replace(case, runtimes={})], parent, [(0, 0, 0)])


def test_budget_episode_rejects_missing_invalid_runtime_and_land_completion() -> None:
    candidate = TaskCandidate(_task())
    with pytest.raises(ValueError, match="missing compile runtime"):
        simulate_task_budget_episode(
            [candidate], _context("vtol"), "vtol", runtimes={}, airframe_profile_hash=PROFILE_HASH
        )
    with pytest.raises(ValueError):
        simulate_task_budget_episode(
            [candidate],
            _context("vtol"),
            "vtol",
            runtimes={candidate.task.task_id: {}},
            airframe_profile_hash=PROFILE_HASH,
        )
    with pytest.raises(ValueError, match="RETURN"):
        simulate_task_budget_episode(
            [TaskCandidate(replace(_task(), completion_action=PlanAction.LAND))],
            _context("vtol"),
            "vtol",
            runtimes={},
            airframe_profile_hash=PROFILE_HASH,
        )


def test_budget_episode_empty_and_unhealthy_have_no_projected_execution() -> None:
    context = replace(_context("vtol"), gps_fix_type=0)
    empty = simulate_task_budget_episode(
        [], context, "vtol", runtimes={}, airframe_profile_hash=PROFILE_HASH
    )
    assert empty.steps == () and empty.final_context == context
    result = simulate_task_budget_episode(
        [TaskCandidate(_task())],
        context,
        "vtol",
        runtimes={_task().task_id: {}},
        airframe_profile_hash=PROFILE_HASH,
    )
    assert result.steps == () and result.final_context == context
    assert len(result.rejected) == 1


PROFILE_HASH = "b" * 64


def _task() -> ResolvedTaskSpec:
    return ResolvedTaskSpec(
        task_id="shared-corridor-001",
        task_type=TaskType.CORRIDOR_VISUAL_SEARCH,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="corridor-001",
            direction="northbound",
            length_m=1_000.0,
            buffer_m=30.0,
            centerline=(
                GeoPoint(1.3000, 103.8000),
                GeoPoint(1.3040, 103.8000),
            ),
            resolution_confidence=0.95,
        ),
        target_classes=("person",),
        on_detection=(
            PlanAction.OBSERVE_TARGET,
            PlanAction.CAPTURE_EVIDENCE,
            PlanAction.REPORT_GEOLOCATION,
        ),
        constraints=TaskConstraints(40.0, 900.0, True),
        completion_action=PlanAction.RETURN,
    )


def _context(vehicle_class: str) -> ContextSnapshot:
    profile = {
        "vtol": "px4_vtol_gazebo_v1",
        "fixed-wing": "px4_fixed_wing_gazebo_v1",
        "multirotor": "px4_multirotor_gazebo_v1",
    }[vehicle_class]
    return ContextSnapshot(
        snapshot_id=f"shared-context-{vehicle_class}",
        captured_at_s=10.0,
        captured_at_utc="2026-09-04T00:00:00Z",
        aircraft_position=GeoPoint(1.3000, 103.8000),
        home_position=GeoPoint(1.3000, 103.8000),
        airframe_profile_id=profile,
        battery_remaining_pct=90.0,
        estimated_flight_time_s=1_800.0,
        ground_speed_mps=15.0 if vehicle_class == "fixed-wing" else 0.0,
        airspeed_mps=17.0 if vehicle_class == "fixed-wing" else 0.0,
        wind_north_mps=2.0,
        wind_east_mps=-1.0,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.0,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )


def _geometry_and_policy(
    vehicle_class: str,
) -> tuple[
    VtolMissionGeometry | FixedWingMissionGeometry | MultirotorMissionGeometry,
    Px4MissionSafetyPolicy | Px4FixedWingSafetyPolicy | Px4MultirotorSafetyPolicy,
]:
    home = GeoPoint(1.3000, 103.8000)
    if vehicle_class == "vtol":
        geometry = VtolMissionGeometry(
            home=home,
            waypoints=(
                GeoPoint(1.3004, 103.8002, 45.0, "relative_home"),
                GeoPoint(1.3008, 103.8004, 50.0, "relative_home"),
            ),
            takeoff_altitude_m=30.0,
            loiter_time_s=20.0,
            loiter_radius_m=35.0,
            acceptance_radius_m=5.0,
            landing=GeoPoint(1.3000, 103.8000, 0.0, "relative_home"),
        )
        policy = Px4MissionSafetyPolicy(
            geofence_center=home,
            geofence_radius_m=2_000.0,
            allowed_airframe_profile_ids=("px4_vtol_gazebo_v1",),
            minimum_relative_altitude_m=10.0,
            maximum_relative_altitude_m=120.0,
            maximum_mission_radius_m=1_000.0,
            maximum_loiter_radius_m=100.0,
            maximum_landing_offset_m=10.0,
        )
        return geometry, policy
    if vehicle_class == "multirotor":
        geometry = MultirotorMissionGeometry(
            home=home,
            waypoints=(
                GeoPoint(1.3004, 103.8002, 35.0, "relative_home"),
                GeoPoint(1.3008, 103.8004, 40.0, "relative_home"),
            ),
            takeoff_altitude_m=30.0,
            loiter_time_s=15.0,
            loiter_radius_m=25.0,
            acceptance_radius_m=5.0,
            landing=GeoPoint(1.3000, 103.8000, 0.0, "relative_home"),
        )
        policy = Px4MultirotorSafetyPolicy(
            geofence_center=home,
            geofence_radius_m=2_000.0,
            allowed_airframe_profile_ids=("px4_multirotor_gazebo_v1",),
            minimum_relative_altitude_m=10.0,
            maximum_relative_altitude_m=100.0,
            maximum_mission_radius_m=1_000.0,
            maximum_loiter_radius_m=100.0,
            maximum_landing_offset_m=10.0,
        )
        return geometry, policy
    geometry = FixedWingMissionGeometry(
        home=home,
        waypoints=(
            GeoPoint(1.3010, 103.8000, 60.0, "relative_home"),
            GeoPoint(1.3015, 103.8010, 60.0, "relative_home"),
        ),
        landing_approach=GeoPoint(1.3000, 103.8020, 40.0, "relative_home"),
        landing=GeoPoint(1.3000, 103.8000, 0.0, "relative_home"),
        takeoff_altitude_m=35.0,
        acceptance_radius_m=8.0,
    )
    policy = Px4FixedWingSafetyPolicy(
        geofence_center=home,
        geofence_radius_m=6_000.0,
        allowed_airframe_profile_ids=("px4_fixed_wing_gazebo_v1",),
    )
    return geometry, policy


@pytest.mark.parametrize("vehicle_class", ("vtol", "fixed-wing", "multirotor"))
def test_planner_output_flows_through_canonical_px4_compiler(vehicle_class: str) -> None:
    task = _task()
    context = _context(vehicle_class)
    geometry, policy = _geometry_and_policy(vehicle_class)

    result = compile_planned_task_mission(
        task,
        context,
        vehicle_class,
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
        mission_id=f"{task.task_id}-{vehicle_class}",
    )

    assert result.vehicle_class == vehicle_class
    assert result.plan == plan_task(task, context, vehicle_class)
    assert result.estimate.task_id == task.task_id
    assert (
        result.compiled_mission.executable_mission.platform_plan_hash == result.plan.canonical_hash
    )
    assert (
        result.executable_mission_hash == result.compiled_mission.executable_mission.canonical_hash
    )
    assert result.wire_hash == result.compiled_mission.wire_hash


@pytest.mark.parametrize("vehicle_class", ("vtol", "fixed-wing", "multirotor"))
def test_vehicle_neutral_campaign_seam_uses_shared_policy_and_compiler(
    vehicle_class: str,
) -> None:
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker

    task = _task()
    context = _context(vehicle_class)
    geometry, safety_policy = _geometry_and_policy(vehicle_class)
    runtime = {
        "schema_version": 1,
        "vehicle_class": vehicle_class,
        **geometry.to_mapping(),
        "safety_policy": safety_policy.to_mapping(),
    }
    ranker = TaskRanker((0, 0, 0), f"vehicle-neutral-{vehicle_class}")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=PROFILE_HASH,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )

    binding = select_and_compile_task(
        [TaskCandidate(task)],
        context,
        vehicle_class,
        runtimes={task.task_id: runtime},
        airframe_profile_hash=PROFILE_HASH,
        shared_policy=SharedTaskPolicy(ranker, artifact),
        mission_id=f"vehicle-neutral-{vehicle_class}",
    )

    assert isinstance(binding, CampaignTaskBinding)
    assert binding.decision is not None
    assert binding.decision.action.value == "select"
    assert binding.selected_task_id == task.task_id
    assert binding.planner_revalidated is True
    assert binding.mission_bound is True
    assert binding.compilation is not None
    assert binding.compilation.vehicle_class == vehicle_class
    assert binding.compilation.plan == binding.selected.plan


def test_vehicle_neutral_campaign_rejects_shared_policy_identity_drift(monkeypatch) -> None:
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker

    task = _task()
    context = _context("multirotor")
    geometry, safety_policy = _geometry_and_policy("multirotor")
    runtime = {
        "schema_version": 1,
        "vehicle_class": "multirotor",
        **geometry.to_mapping(),
        "safety_policy": safety_policy.to_mapping(),
    }
    ranker = TaskRanker((0, 0, 0), "vehicle-neutral-drift")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=PROFILE_HASH,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    shared = SharedTaskPolicy(ranker, artifact)
    original_decide = SharedTaskPolicy.decide

    def drifted_decide(self, entries, snapshot, vehicle):
        decision, selected = original_decide(self, entries, snapshot, vehicle)
        return replace(decision, truth_free=False), selected

    monkeypatch.setattr(SharedTaskPolicy, "decide", drifted_decide)
    with pytest.raises(ValueError, match="shared policy decision identity mismatch"):
        select_and_compile_task(
            [TaskCandidate(task)],
            context,
            "multirotor",
            runtimes={task.task_id: runtime},
            airframe_profile_hash=PROFILE_HASH,
            shared_policy=shared,
        )


def test_vehicle_neutral_campaign_seam_abstains_without_binding_mission() -> None:
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker

    task = _task()
    context = replace(_context("multirotor"), gps_fix_type=0)
    geometry, safety_policy = _geometry_and_policy("multirotor")
    ranker = TaskRanker((0, 0, 0), "vehicle-neutral-abstain")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash=PROFILE_HASH,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )

    binding = select_and_compile_task(
        [TaskCandidate(task)],
        context,
        "multirotor",
        runtimes={
            task.task_id: {
                "schema_version": 1,
                "vehicle_class": "multirotor",
                **geometry.to_mapping(),
                "safety_policy": safety_policy.to_mapping(),
            }
        },
        airframe_profile_hash=PROFILE_HASH,
        shared_policy=SharedTaskPolicy(ranker, artifact),
    )

    assert binding.selected is None
    assert binding.compilation is None
    assert binding.mission_bound is False
    assert binding.decision is not None
    assert binding.decision.action.value == "abstain"


def test_planned_compilation_rejects_cross_bound_artifacts() -> None:
    task = _task()
    context = _context("multirotor")
    geometry, policy = _geometry_and_policy("multirotor")
    result = compile_planned_task_mission(
        task,
        context,
        "multirotor",
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
    )

    with pytest.raises(ValueError, match="compiled mission is not bound"):
        type(result)(
            vehicle_class=result.vehicle_class,
            estimate=result.estimate,
            plan=plan_task(task, replace(context, snapshot_id="different-context"), "multirotor"),
            compiled_mission=result.compiled_mission,
        )


def test_planned_compilation_round_trips_wire_artifact_and_hashes() -> None:
    task = _task()
    context = _context("fixed-wing")
    geometry, policy = _geometry_and_policy("fixed-wing")
    result = compile_planned_task_mission(
        task,
        context,
        "fixed-wing",
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
    )

    reopened = PlannedMissionCompilation.from_mapping(result.to_mapping())

    assert reopened == result
    assert reopened.compiled_mission.wire_items == result.compiled_mission.wire_items


def test_shared_mission_loader_accepts_bare_compiled_and_planner_documents(tmp_path) -> None:
    task = _task()
    context = _context("multirotor")
    geometry, policy = _geometry_and_policy("multirotor")
    result = compile_planned_task_mission(
        task,
        context,
        "multirotor",
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
    )
    mission = result.compiled_mission.executable_mission
    documents = {
        "bare.json": mission.to_mapping(),
        "compiled.json": result.compiled_mission.to_mapping(),
        "planned.json": result.to_mapping(),
    }

    for name, document in documents.items():
        path = tmp_path / name
        path.write_text(json.dumps(document), encoding="utf-8")
        assert load_executable_mission_document(path) == mission


def test_planned_compilation_rejects_tampered_declared_hash_and_unknown_field() -> None:
    task = _task()
    context = _context("vtol")
    geometry, policy = _geometry_and_policy("vtol")
    result = compile_planned_task_mission(
        task,
        context,
        "vtol",
        geometry=geometry,
        policy=policy,
        airframe_profile_hash=PROFILE_HASH,
    )

    tampered_hash = result.to_mapping()
    tampered_hash["wire_hash"] = "0" * 64
    with pytest.raises(ValueError, match="wire_hash does not match"):
        PlannedMissionCompilation.from_mapping(tampered_hash)

    unknown_field = result.to_mapping()
    unknown_field["unexpected"] = True
    with pytest.raises(ValueError, match="unknown field"):
        PlannedMissionCompilation.from_mapping(unknown_field)


def test_runtime_compile_adapter_round_trips_geometry_and_policy_for_all_airframes() -> None:
    task = _task()
    for vehicle_class in ("vtol", "fixed-wing", "multirotor"):
        context = _context(vehicle_class)
        geometry, policy = _geometry_and_policy(vehicle_class)
        runtime = {
            "schema_version": MISSION_COMPILE_RUNTIME_SCHEMA_VERSION,
            "vehicle_class": vehicle_class,
            **geometry.to_mapping(),
            "safety_policy": policy.to_mapping(),
        }

        parsed_geometry, parsed_policy = parse_mission_compile_runtime(runtime, vehicle_class)
        assert parsed_geometry == geometry
        assert parsed_policy == policy
        result = compile_planned_task_mission_from_runtime(
            task,
            context,
            vehicle_class,
            runtime=runtime,
            airframe_profile_hash=PROFILE_HASH,
        )
        assert result.vehicle_class == vehicle_class
        assert result.wire_hash == result.compiled_mission.wire_hash


def test_runtime_compile_adapter_rejects_mismatched_vehicle_and_truth() -> None:
    geometry, policy = _geometry_and_policy("vtol")
    runtime = {
        "schema_version": MISSION_COMPILE_RUNTIME_SCHEMA_VERSION,
        "vehicle_class": "fixed-wing",
        **geometry.to_mapping(),
        "safety_policy": policy.to_mapping(),
    }
    with pytest.raises(ValueError, match="does not match"):
        parse_mission_compile_runtime(runtime, "vtol")

    truth_runtime = {
        **runtime,
        "vehicle_class": "vtol",
        "truth": {"target": "evaluator-only"},
    }
    with pytest.raises(ValueError, match="unknown field"):
        parse_mission_compile_runtime(truth_runtime, "vtol")


def test_mission_compile_cli_writes_reopenable_artifact(tmp_path) -> None:
    task = _task()
    context = _context("fixed-wing")
    geometry, policy = _geometry_and_policy("fixed-wing")
    task_path = tmp_path / "task.json"
    context_path = tmp_path / "context.json"
    runtime_path = tmp_path / "runtime.json"
    output_path = tmp_path / "out" / "compiled.json"
    task_path.write_text(json.dumps(task.to_mapping()), encoding="utf-8")
    context_path.write_text(json.dumps(context.to_mapping()), encoding="utf-8")
    runtime_path.write_text(
        json.dumps(
            {
                "schema_version": MISSION_COMPILE_RUNTIME_SCHEMA_VERSION,
                "vehicle_class": "fixed-wing",
                **geometry.to_mapping(),
                "safety_policy": policy.to_mapping(),
            }
        ),
        encoding="utf-8",
    )

    assert (
        mission_compile_cli_main(
            [
                "--task",
                str(task_path),
                "--context",
                str(context_path),
                "--runtime",
                str(runtime_path),
                "--vehicle-class",
                "fixed_wing",
                "--airframe-profile-hash",
                PROFILE_HASH,
                "--mission-id",
                "cli-fixed-wing-001",
                "--out",
                str(output_path),
            ]
        )
        == 0
    )
    reopened = PlannedMissionCompilation.from_mapping(
        json.loads(output_path.read_text(encoding="utf-8"))
    )
    assert reopened.compiled_mission.executable_mission.mission_id == "cli-fixed-wing-001"
    assert reopened.vehicle_class == "fixed-wing"


def test_policy_mapping_rejects_unknown_fields() -> None:
    _geometry, policy = _geometry_and_policy("multirotor")
    raw = policy.to_mapping()
    raw["unexpected"] = True
    with pytest.raises(ValueError, match="unknown field"):
        Px4MultirotorSafetyPolicy.from_mapping(raw)
