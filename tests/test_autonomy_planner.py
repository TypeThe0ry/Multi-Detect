from __future__ import annotations

import json
from dataclasses import replace

import pytest

from multidetect.autonomy import (
    ContextSnapshot,
    GeoPoint,
    PlanAction,
    PlannerError,
    PlanningEstimate,
    PlatformNeutralPlan,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
    estimate_task,
    normalize_vehicle_class,
    plan_task,
)
from multidetect.autonomy.planner_cli import main as planner_cli_main
from multidetect.autonomy.task_selection import TaskCandidate, select_next_task


def test_ranker_extends_baseline_and_never_selects_infeasible_task(tmp_path):
    import hashlib

    from multidetect.autonomy.task_ranker import TaskRanker, load_task_ranker

    short = _task(task_id="short")
    long = replace(short, task_id="long", geometry=replace(short.geometry, length_m=3000))
    impossible = replace(
        short, task_id="impossible", geometry=replace(short.geometry, length_m=1e7)
    )
    candidates = [TaskCandidate(short), TaskCandidate(long), TaskCandidate(impossible, 999)]
    context = _context()
    baseline = TaskRanker((0, 0, 0), "untrained fixture")
    assert baseline.select(candidates, context, "vtol") == select_next_task(
        candidates, context, "vtol"
    ).selected
    ranker = TaskRanker((0, 1, 0), "untrained fixture preferring longer feasible routes")
    assert ranker.select(candidates, context, "vtol").candidate.task.task_id == "long"
    assert ranker.select(candidates, _context(gps_fix_type=0), "vtol") is None
    raw = json.dumps(ranker.to_mapping()).encode()
    path = tmp_path / "candidate.json"
    path.write_bytes(raw)
    restored = load_task_ranker(path, expected_sha256=hashlib.sha256(raw).hexdigest())
    assert restored == ranker
    assert restored.canonical_hash == ranker.canonical_hash
    with pytest.raises(ValueError, match="hash mismatch"):
        load_task_ranker(path, expected_sha256="0" * 64)


def test_shared_policy_artifact_binds_ranker_and_revalidates_selection():
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import PolicyAction, SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker

    ranker = TaskRanker((0, 0, 0), "shared-policy-test")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.9",
        airframe_profile_hash="0" * 64,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    policy = SharedTaskPolicy(ranker, artifact)
    decision, selected = policy.decide(
        [TaskCandidate(_task(task_id="b")), TaskCandidate(_task(task_id="a"))],
        _context(),
        "vtol",
    )
    assert decision.action is PolicyAction.SELECT
    assert decision.planner_revalidated is True
    assert decision.policy_artifact_sha256 == artifact.canonical_hash
    assert selected is not None and selected.candidate.task.task_id == "a"
    assert policy.observe([], _context(), "vtol").payload["truth_free"] is True


def test_shared_policy_rejects_explicit_context_profile_mismatch():
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker

    ranker = TaskRanker((0, 0, 0), "shared-policy-profile-mismatch")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.9",
        airframe_profile_hash="0" * 64,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    policy = SharedTaskPolicy(ranker, artifact)
    mismatched = _context(airframe_profile_hash="f" * 64)
    with pytest.raises(ValueError, match="context airframe profile mismatch"):
        policy.decide([TaskCandidate(_task())], mismatched, "vtol")
    with pytest.raises(ValueError, match="context airframe profile mismatch"):
        policy.observe([TaskCandidate(_task())], mismatched, "vtol")


def test_policy_artifact_roundtrip_requires_ranker_identity(tmp_path):
    import hashlib

    from multidetect.autonomy.policy_artifact import PolicyArtifact, load_policy_artifact
    from multidetect.autonomy.task_ranker import TaskRanker

    ranker = TaskRanker((0, 0, 0), "artifact-roundtrip")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash="0" * 64,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
        parent_model_sha256="3" * 64,
        training_dataset_sha256="4" * 64,
    )
    path = tmp_path / "policy-artifact.json"
    raw = (json.dumps(artifact.to_mapping(), sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    loaded = load_policy_artifact(
        path,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        ranker=ranker,
    )
    assert loaded == artifact
    with pytest.raises(ValueError, match="ranker hash"):
        load_policy_artifact(
            path,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            ranker=TaskRanker((1, 0, 0), "different"),
        )


def test_policy_artifact_loader_rejects_oversized_file(tmp_path):
    import hashlib

    from multidetect.autonomy.policy_artifact import load_policy_artifact
    from multidetect.autonomy.task_ranker import TaskRanker

    path = tmp_path / "oversized-policy-artifact.json"
    raw = b"{" + b" " * 65536 + b"}"
    path.write_bytes(raw)
    with pytest.raises(ValueError, match="size limit"):
        load_policy_artifact(
            path,
            expected_sha256=hashlib.sha256(raw).hexdigest(),
            ranker=TaskRanker((0, 0, 0), "size-limit"),
        )


def test_shared_policy_abstains_without_feasible_task():
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import PolicyAction, SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker

    ranker = TaskRanker((0, 0, 0), "shared-policy-abstain-test")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.9",
        airframe_profile_hash="0" * 64,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    decision, selected = SharedTaskPolicy(ranker, artifact).decide(
        [TaskCandidate(_task())], _context(gps_fix_type=0), "vtol"
    )
    assert decision.action is PolicyAction.ABSTAIN
    assert decision.reason == "no_feasible_task"
    assert selected is None


def test_policy_observation_does_not_expose_mutable_nested_payload():
    from multidetect.autonomy.policy_artifact import PolicyArtifact
    from multidetect.autonomy.policy_contract import SharedTaskPolicy
    from multidetect.autonomy.task_ranker import TaskRanker

    ranker = TaskRanker((0, 0, 0), "observation-copy-test")
    artifact = PolicyArtifact.for_ranker(
        ranker,
        policy_id="task-policy",
        policy_version="0.3.0-pre.8",
        airframe_profile_hash="0" * 64,
        mission_contract_hash="1" * 64,
        safety_policy_hash="2" * 64,
    )
    observation = SharedTaskPolicy(ranker, artifact).observe(
        [TaskCandidate(_task())], _context(), "vtol"
    )
    mapping = observation.to_mapping()
    mapping["base_observation"]["feasible"].clear()
    assert observation.to_mapping()["base_observation"]["feasible"]


@pytest.mark.parametrize(
    "mutation", ["extra", "nan", "bool", "base", "missing", "size", "duplicate"]
)
def test_ranker_loader_rejects_invalid_artifacts(tmp_path, mutation):
    import hashlib

    from multidetect.autonomy.task_ranker import TaskRanker, load_task_ranker

    document = TaskRanker((0, 0, 0), "untrained fixture").to_mapping()
    if mutation == "extra":
        document["execute"] = "not allowed"
    elif mutation == "nan":
        document["coefficients"]["priority"] = float("nan")
    elif mutation == "bool":
        document["coefficients"]["priority"] = True
    elif mutation == "base":
        document["base"] = "different planner"
    elif mutation == "missing":
        document["coefficients"].pop("priority")
    raw = json.dumps(document).encode()
    if mutation == "size":
        raw += b" " * 65537
    elif mutation == "duplicate":
        raw = b'{"schema_version":1,' + raw[1:]
    path = tmp_path / "invalid.json"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        load_task_ranker(path, expected_sha256=hashlib.sha256(raw).hexdigest())


def test_policy_baseline_preserves_plan_and_roundtrips_action():
    from multidetect.autonomy.task_policy import (
        TaskPolicyAction,
        baseline_policy_action,
        build_policy_observation,
        resolve_policy_action,
    )

    candidates = [TaskCandidate(_task(task_id="b")), TaskCandidate(_task(task_id="a"))]
    context = _context()
    action = baseline_policy_action(candidates, context, "vtol")
    assert action == TaskPolicyAction.from_mapping(action.to_mapping())
    assert action.task_id == "a"
    assert resolve_policy_action(action, candidates, context, "vtol") == select_next_task(
        candidates, context, "vtol"
    ).selected
    assert build_policy_observation(candidates, context, "vtol") == build_policy_observation(
        reversed(candidates), context, "vtol"
    )


@pytest.mark.parametrize("change", ["context", "task", "priority", "vehicle", "infeasible"])
def test_policy_action_rejects_changed_inputs_and_infeasible_selection(change):
    from multidetect.autonomy.task_policy import baseline_policy_action, resolve_policy_action

    candidates = [TaskCandidate(_task(task_id="a"))]
    context, vehicle = _context(), "vtol"
    action = baseline_policy_action(candidates, context, vehicle)
    if change == "context":
        context = replace(context, snapshot_id="new-observation")
    elif change == "task":
        candidates = [TaskCandidate(_task(task_id="b"))]
    elif change == "priority":
        candidates = [TaskCandidate(candidates[0].task, 10)]
    elif change == "vehicle":
        vehicle = "multirotor"
    elif change == "infeasible":
        action = replace(action, task_id="unknown")
    with pytest.raises(ValueError):
        resolve_policy_action(action, candidates, context, vehicle)


def test_policy_abstention_and_empty_feasible_set():
    from multidetect.autonomy.task_policy import baseline_policy_action, resolve_policy_action

    candidates = [TaskCandidate(_task())]
    context = _context(gps_fix_type=0)
    action = baseline_policy_action(candidates, context, "vtol")
    assert action.task_id is None
    assert resolve_policy_action(action, candidates, context, "vtol") is None
    context = _context()
    action = replace(baseline_policy_action(candidates, context, "vtol"), task_id=None)
    assert resolve_policy_action(action, candidates, context, "vtol") is None


@pytest.mark.parametrize("field,value", [("schema_version", True), ("schema_version", 2),
                                         ("task_id", 1), ("observation_sha256", "bad"),
                                         ("score", 100)])
def test_policy_action_strict_fields(field, value):
    from multidetect.autonomy.task_policy import TaskPolicyAction

    raw = {"schema_version": 1, "task_id": None, "observation_sha256": "a" * 64}
    raw[field] = value
    with pytest.raises(ValueError):
        TaskPolicyAction.from_mapping(raw)


def test_next_task_preserves_existing_plan_and_deterministic_ties() -> None:
    first = TaskCandidate(_task(task_id="a"))
    second = TaskCandidate(_task(task_id="b"))
    result = select_next_task([second, first], _context(), "vtol")
    assert result == select_next_task([first, second], _context(), "vtol")
    assert result.selected is not None
    assert result.selected.candidate == first
    assert result.selected.plan == plan_task(first.task, _context(), "vtol")
    assert result.context_snapshot_hash == _context().canonical_hash


def test_next_task_priority_then_duration_and_rejection() -> None:
    short = _task(task_id="short")
    long = replace(short, task_id="long", geometry=replace(short.geometry, length_m=3000))
    impossible = replace(
        short, task_id="impossible", geometry=replace(short.geometry, length_m=1e7)
    )
    result = select_next_task(
        [TaskCandidate(long), TaskCandidate(short), TaskCandidate(impossible, 999)],
        _context(),
        "vtol",
    )
    assert [item.candidate.task.task_id for item in result.ranked] == ["short", "long"]
    assert result.rejected[0].task_id == "impossible"
    assert "energy budget" in result.rejected[0].reason
    priority_result = select_next_task(
        [TaskCandidate(long, 1), TaskCandidate(short)], _context(), "vtol"
    )
    assert priority_result.selected.candidate.task.task_id == "long"


@pytest.mark.parametrize(
    "changes",
    [
        {"gps_fix_type": 0},
        {"geofence_valid": False},
        {"battery_remaining_pct": 10.0},
        {"perception_available": False},
    ],
)
def test_next_task_cannot_rank_around_safety_gates(changes: dict) -> None:
    result = select_next_task([TaskCandidate(_task(), 999)], _context(**changes), "vtol")
    assert result.selected is None
    assert len(result.rejected) == 1
    assert result.rejected[0].reason


def test_next_task_empty_and_duplicate_input() -> None:
    assert select_next_task([], _context(), "vtol").selected is None
    candidate = TaskCandidate(_task())
    with pytest.raises(PlannerError, match="unique"):
        select_next_task([candidate, candidate], _context(), "vtol")
    with pytest.raises(ValueError):
        TaskCandidate(_task(), True)
    with pytest.raises(ValueError):
        TaskCandidate(_task(), -1)


def _context(**changes: object) -> ContextSnapshot:
    context = ContextSnapshot(
        snapshot_id="planner-context-001",
        captured_at_s=10.0,
        captured_at_utc="2026-09-04T00:00:00Z",
        aircraft_position=GeoPoint(1.3, 103.8),
        home_position=GeoPoint(1.3, 103.8),
        airframe_profile_id="px4_vtol_gazebo_v1",
        battery_remaining_pct=86.0,
        estimated_flight_time_s=2100.0,
        ground_speed_mps=0.0,
        airspeed_mps=0.0,
        wind_north_mps=0.0,
        wind_east_mps=0.0,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.4,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )
    return replace(context, **changes)


def _task(
    task_type: TaskType = TaskType.CORRIDOR_VISUAL_SEARCH,
    *,
    on_detection: tuple[PlanAction, ...] = (
        PlanAction.OBSERVE_TARGET,
        PlanAction.CAPTURE_EVIDENCE,
        PlanAction.REPORT_GEOLOCATION,
    ),
    **changes: object,
) -> ResolvedTaskSpec:
    task = ResolvedTaskSpec(
        task_id="river-search-001",
        task_type=task_type,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="river-north-001",
            direction="northbound",
            length_m=2_000.0,
            buffer_m=35.0,
            centerline=(GeoPoint(1.3, 103.8), GeoPoint(1.3085, 103.8012)),
            resolution_confidence=0.94,
        ),
        target_classes=("person_in_water", "red_life_jacket"),
        on_detection=on_detection,
        constraints=TaskConstraints(40.0, 900.0, True),
        completion_action=PlanAction.RETURN,
    )
    return replace(task, **changes)


@pytest.mark.parametrize(
    ("task_type", "primary_action", "on_detection"),
    (
        (TaskType.AREA_SEARCH, PlanAction.SEARCH_AREA, (PlanAction.OBSERVE_TARGET,)),
        (
            TaskType.CORRIDOR_VISUAL_SEARCH,
            PlanAction.SEARCH_CORRIDOR,
            (PlanAction.OBSERVE_TARGET, PlanAction.CAPTURE_EVIDENCE),
        ),
        (TaskType.TARGET_OBSERVATION, PlanAction.OBSERVE_TARGET, (PlanAction.CAPTURE_EVIDENCE,)),
        (TaskType.SURVEY, PlanAction.SURVEY, ()),
    ),
)
def test_plan_covers_all_task_types(
    task_type: TaskType,
    primary_action: PlanAction,
    on_detection: tuple[PlanAction, ...],
) -> None:
    plan = plan_task(_task(task_type, on_detection=on_detection), _context(), "vtol")

    assert tuple(step.sequence for step in plan.steps) == tuple(range(len(plan.steps)))
    assert [step.action for step in plan.steps] == [
        PlanAction.TAKEOFF,
        PlanAction.TRANSIT,
        primary_action,
        *on_detection,
        PlanAction.RETURN,
        PlanAction.LAND,
    ]
    assert plan.plan_id == "river-search-001:platform-neutral-v1"
    assert plan.task_spec_hash == _task(task_type, on_detection=on_detection).canonical_hash


def test_plan_is_byte_identical_across_airframe_classes() -> None:
    task = _task()
    context = _context()
    plans = [plan_task(task, context, vehicle) for vehicle in ("vtol", "fixed-wing", "multirotor")]

    assert plans[0].steps == plans[1].steps == plans[2].steps
    assert plans[0].canonical_hash == plans[1].canonical_hash == plans[2].canonical_hash


def test_estimate_uses_wgs84_polyline_and_adverse_wind() -> None:
    calm = estimate_task(_task(), _context(), "fixed-wing")
    windy = estimate_task(_task(), _context(wind_north_mps=2.0, wind_east_mps=2.0), "fixed-wing")

    assert calm.task_distance_m >= 2_000.0
    assert calm.total_distance_m == pytest.approx(
        calm.outbound_distance_m + calm.task_distance_m + calm.return_distance_m
    )
    understated_task = replace(
        _task(),
        geometry=replace(_task().geometry, length_m=100.0),
    )
    understated_estimate = estimate_task(understated_task, _context(), "fixed-wing")
    plan = plan_task(understated_task, _context(), "fixed-wing")
    task_step = next(step for step in plan.steps if step.action is PlanAction.SEARCH_CORRIDOR)
    assert understated_estimate.task_distance_m > understated_task.geometry.length_m
    assert task_step.quantity == pytest.approx(understated_estimate.task_distance_m)
    assert windy.cruise_speed_mps < calm.cruise_speed_mps
    assert windy.estimated_duration_s > calm.estimated_duration_s


def test_estimate_uses_directional_wind_triangle_and_fails_closed() -> None:
    calm = estimate_task(_task(), _context(), "fixed-wing")
    crosswind = estimate_task(
        _task(),
        _context(wind_north_mps=0.0, wind_east_mps=10.0),
        "fixed-wing",
    )

    # A 10 m/s east wind is mostly crosswind for this northbound route.  It
    # must be slower than calm air, but it must not be penalized as if the
    # whole vector were a headwind (18 - 10 = 8 m/s).
    assert 8.0 < crosswind.cruise_speed_mps < calm.cruise_speed_mps

    with pytest.raises(PlannerError, match="crosswind"):
        estimate_task(
            _task(),
            _context(wind_north_mps=0.0, wind_east_mps=25.0),
            "fixed-wing",
        )

    with pytest.raises(PlannerError, match="forward progress"):
        estimate_task(
            replace(_task(), completion_action=PlanAction.LAND),
            _context(wind_north_mps=-18.0),
            "fixed-wing",
        )


@pytest.mark.parametrize(
    ("token", "expected"),
    (
        ("VTOL", "vtol"),
        ("fixed_wing", "fixed-wing"),
        ("airplane", "fixed-wing"),
        ("x500", "multirotor"),
    ),
)
def test_vehicle_aliases_are_shared_and_strict(token: str, expected: str) -> None:
    assert normalize_vehicle_class(token) == expected

    with pytest.raises(PlannerError, match="vehicle_class"):
        normalize_vehicle_class("unknown-airframe")


@pytest.mark.parametrize(
    ("changes", "message"),
    (
        ({"gps_fix_type": 2}, "GPS fix"),
        ({"gps_horizontal_accuracy_m": 7.0}, "GPS horizontal accuracy"),
        ({"geofence_valid": False}, "geofence"),
        ({"camera_available": False}, "camera"),
        ({"perception_available": False}, "perception"),
        ({"map_cache_available": False}, "map cache"),
        ({"dem_available": False}, "DEM"),
        ({"battery_remaining_pct": 45.0}, "battery"),
        ({"estimated_flight_time_s": 0.0}, "estimated flight time"),
    ),
)
def test_planner_gates_fail_closed(changes: dict[str, object], message: str) -> None:
    with pytest.raises(PlannerError, match=message):
        plan_task(_task(), _context(**changes), "multirotor")


def test_planner_rejects_unresolved_low_confidence_and_bad_actions() -> None:
    with pytest.raises(PlannerError, match="unresolved assumptions"):
        plan_task(_task(unresolved_assumptions=("choose feature",)), _context(), "vtol")

    low_confidence = replace(
        _task(),
        geometry=replace(_task().geometry, resolution_confidence=0.2),
    )
    with pytest.raises(PlannerError, match="geometry confidence"):
        plan_task(low_confidence, _context(), "vtol")

    bad_action = _task(on_detection=(PlanAction.RETURN,))
    with pytest.raises(PlannerError, match="on_detection action"):
        plan_task(bad_action, _context(), "vtol")

    duplicate_action = _task(on_detection=(PlanAction.OBSERVE_TARGET, PlanAction.OBSERVE_TARGET))
    with pytest.raises(PlannerError, match="unique"):
        plan_task(duplicate_action, _context(), "vtol")


def test_planner_enforces_time_and_energy_budget() -> None:
    with pytest.raises(PlannerError, match="maximum_duration"):
        plan_task(
            replace(
                _task(),
                constraints=TaskConstraints(40.0, 100.0, True),
            ),
            _context(),
            "fixed-wing",
        )

    with pytest.raises(PlannerError, match="energy budget"):
        plan_task(_task(), _context(battery_remaining_pct=51.0), "multirotor")


def test_land_completion_does_not_add_return_step() -> None:
    task = replace(_task(), completion_action=PlanAction.LAND)
    plan = plan_task(task, _context(), "multirotor")
    estimate = estimate_task(task, _context(), "multirotor")

    assert plan.steps[-1].action is PlanAction.LAND
    assert all(step.action is not PlanAction.RETURN for step in plan.steps)
    assert estimate.return_distance_m == 0.0


def test_planner_cli_writes_schema_compatible_plan_and_estimate(tmp_path) -> None:
    task_path = tmp_path / "task.json"
    context_path = tmp_path / "context.json"
    plan_path = tmp_path / "out" / "plan.json"
    estimate_path = tmp_path / "out" / "estimate.json"
    task_path.write_text(
        json.dumps(_task().to_mapping(), ensure_ascii=False),
        encoding="utf-8",
    )
    context_path.write_text(
        json.dumps(_context().to_mapping()),
        encoding="utf-8",
    )

    assert (
        planner_cli_main(
            [
                "--task",
                str(task_path),
                "--context",
                str(context_path),
                "--vehicle-class",
                "fixed_wing",
                "--out",
                str(plan_path),
                "--estimate-out",
                str(estimate_path),
            ]
        )
        == 0
    )
    plan_mapping = json.loads(plan_path.read_text(encoding="utf-8"))
    estimate_mapping = json.loads(estimate_path.read_text(encoding="utf-8"))
    parsed_plan = PlatformNeutralPlan.from_mapping(plan_mapping)
    parsed_estimate = PlanningEstimate.from_mapping(estimate_mapping)
    assert parsed_plan.plan_id == "river-search-001:platform-neutral-v1"
    assert plan_mapping["task_spec_hash"] == _task().canonical_hash
    assert parsed_estimate.vehicle_class == "fixed-wing"


def test_planning_estimate_rejects_unknown_fields() -> None:
    estimate = estimate_task(_task(), _context(), "fixed-wing")
    raw = estimate.to_mapping()
    raw["unexpected"] = True
    with pytest.raises(ValueError, match="unknown field"):
        PlanningEstimate.from_mapping(raw)


def test_planner_cli_returns_nonzero_for_stale_context(tmp_path, capsys) -> None:
    task_path = tmp_path / "task.json"
    context_path = tmp_path / "context.json"
    task_path.write_text(
        json.dumps(_task().to_mapping(), ensure_ascii=False),
        encoding="utf-8",
    )
    context_path.write_text(
        json.dumps(_context(gps_fix_type=2).to_mapping()),
        encoding="utf-8",
    )

    assert (
        planner_cli_main(
            [
                "--task",
                str(task_path),
                "--context",
                str(context_path),
                "--vehicle-class",
                "vtol",
            ]
        )
        == 2
    )
    assert "GPS fix" in capsys.readouterr().err
