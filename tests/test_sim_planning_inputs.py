from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace

import pytest

from multidetect.autonomy.contracts import (
    ContextProvenance,
    ContextSnapshot,
    ContextValueSource,
    GeoPoint,
    PlanAction,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
)
from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.planner import PlannerPolicy, RouteAcquisitionRequirements, estimate_task
from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.autonomy.sim_planning_inputs import (
    MODEL_CONTRACT,
    OBSERVATION_CONTRACT,
    ExplicitSimPlanningModel,
    qualify_sim_planning_context,
    validate_sim_planning_qualification,
)
from multidetect.px4_external_mission import ExternalRuntimeAttestation
from multidetect.simulator.flight_model import DeterministicFlightModel, SimulationEnergyProfile

_MESSAGES = (
    "HEARTBEAT",
    "EXTENDED_SYS_STATE",
    "HOME_POSITION",
    "GPS_RAW_INT",
    "GLOBAL_POSITION_INT",
    "LOCAL_POSITION_NED",
    "BATTERY_STATUS",
    "SYS_STATUS",
    "VFR_HUD",
)


def _context(**kwargs):
    return replace(
        ContextSnapshot(
            schema_version=2,
            snapshot_id="fixture-observation",
            captured_at_s=10.0,
            captured_at_utc="2026-10-08T00:00:00Z",
            aircraft_position=GeoPoint(1.3, 103.8, 0.0, "relative_home"),
            home_position=GeoPoint(1.3, 103.8),
            airframe_profile_id="px4_multirotor_gazebo_v1",
            airframe_profile_hash="a" * 64,
            battery_remaining_pct=90.0,
            estimated_flight_time_s=None,
            ground_speed_mps=0.0,
            airspeed_mps=None,
            wind_north_mps=0.0,
            wind_east_mps=0.0,
            gps_fix_type=3,
            gps_horizontal_accuracy_m=1.0,
            geofence_valid=False,
            camera_available=False,
            perception_available=False,
            map_cache_available=True,
            dem_available=True,
            provenance=ContextProvenance(
                "SIM",
                ContextValueSource("unknown"),
                ContextValueSource("unknown"),
                ContextValueSource("explicit_SIM_wind_model", "b" * 64),
            ),
        ),
        **kwargs,
    )


def _attestation(**kwargs):
    mapping = {
        "container_id": "1" * 64,
        "image": "fixture/px4@sha256:" + "2" * 64,
        "vehicle_class": "multirotor",
        "airframe_profile_id": "px4_multirotor_gazebo_v1",
        "scenario_id": "fixture-scenario",
        "scenario_manifest_sha256": "3" * 64,
        "ownership_checks": {"fixture_only_owned_check": True},
        "telemetry_qualified": True,
        "truth_free": True,
        "hardware_control_enabled": False,
    }
    mapping.update(kwargs)
    return ExternalRuntimeAttestation.from_mapping(mapping)


def _model():
    return {
        "contract": MODEL_CONTRACT,
        "schema_version": 1,
        "runtime_kind": "SIM",
        "vehicle_class": "multirotor",
        "image": "fixture/px4@sha256:" + "2" * 64,
        "scenario_id": "fixture-scenario",
        "scenario_manifest_sha256": "3" * 64,
        "airframe_profile_id": "px4_multirotor_gazebo_v1",
        "airframe_profile_hash": "a" * 64,
        "energy": {
            "kind": "linear_remaining_energy_full_power_v1",
            "capacity_wh": 60,
            "avionics_power_w": 10,
            "propulsion_max_power_w": 350,
            "minimum_battery_pct": 50,
            "maximum_battery_pct": 100,
            "provenance": "fixture engineering assumption, not physical calibration",
            "reserve_deducted": False,
        },
        "capability": {
            "kind": "conservative_sustainable_air_relative_cruise_v1",
            "cruise_airspeed_mps": 3,
            "maximum_wind_speed_mps": 2,
            "provenance": "fixture sustainable cruise assumption",
        },
        "physical_calibration_claimed": False,
    }


def _source(root, path, raw, pointer=""):
    payload = json.dumps(raw, allow_nan=True).encode("utf-8")
    (root / path).write_bytes(payload)
    return EvidenceBinding(
        path, len(payload), hashlib.sha256(payload).hexdigest(), "CPU-fixture-only", pointer
    )


def _setup(root, context=None, model=None, *, observation_mutator=None, pointer=""):
    context = _context() if context is None else context
    source = _source(root, "raw.json", {"CPU_fixture": True}, None)
    observation = {
        "contract": OBSERVATION_CONTRACT,
        "schema_version": 2,
        "context": context.to_mapping(),
        "context_sha256": context.canonical_hash,
        "source_references": [source.to_mapping()],
        "qualification_details": {
            "source_file_bytes_SHA_verified": True,
            "source_identity_before_after_equal": True,
            "qualified_plain_MC_disarmed_on_ground": True,
            "clock_id": "fixture-clock",
            "endurance_reserve_already_deducted": False,
            "accepted_system_component": [1, 1],
            "message_received_at_s": dict.fromkeys(_MESSAGES, context.captured_at_s),
            "resource_observed_at_s": context.captured_at_s,
            "oldest_observation_at_s": context.captured_at_s,
        },
        "claims": {
            "execution_authority": False,
            "hardware_control_enabled": False,
            "mission_approved": False,
        },
    }
    if observation_mutator:
        observation_mutator(observation)
    wrapped = {"payload": observation} if pointer else observation
    observation_source = _source(root, "observation.json", wrapped, pointer)
    model_source = _source(root, "model.json", _model() if model is None else model)
    options = dict(
        runtime_attestation=_attestation(),
        observation_source=observation_source,
        model_source=model_source,
        artifact_root=root,
        now_s=11.0,
        attested_at_s=10.75,
        clock_id="fixture-clock",
    )
    return context, options


def test_exact_derived_context_retains_every_observed_field_and_flag(tmp_path):
    original, options = _setup(tmp_path)
    derived, receipt = qualify_sim_planning_context(original, **options)
    assert original.estimated_flight_time_s is None and original.flight_capability is None
    assert derived.schema_version == 2
    assert derived.airspeed_mps is None and derived.provenance.airspeed.kind == "unknown"
    assert derived.estimated_flight_time_s == 539.0
    assert receipt.modeled_remaining_wh == 54.0
    assert receipt.modeled_endurance_before_age_s == 540.0
    assert receipt.conservative_receive_age_deduction_s == 1.0
    expected = original.to_mapping()
    actual = derived.to_mapping()
    for key in expected:
        if key not in {"estimated_flight_time_s", "provenance", "flight_capability"}:
            assert actual[key] == expected[key], key
    assert (
        not derived.geofence_valid
        and not derived.camera_available
        and not derived.perception_available
    )
    assert derived.flight_capability.cruise_airspeed_mps == 3.0
    assert derived.flight_capability.model_sha256 == options["model_source"].sha256
    assert derived.provenance.endurance.evidence_sha256 == options["model_source"].sha256
    assert receipt.observed_context_sha256 == original.canonical_hash
    assert receipt.qualified_context_sha256 == derived.canonical_hash
    assert receipt.runtime_attestation_sha256 == options["runtime_attestation"].canonical_hash
    assert receipt.observation_receipt_sha256 == options["observation_source"].sha256
    assert receipt.to_mapping()["reserve_deducted"] is False
    assert all(v is False for v in receipt.to_mapping()["claims"].values())
    assert (
        ExplicitSimPlanningModel.from_mapping(_model()).canonical_hash
        == receipt.model_canonical_sha256
    )


def test_matches_existing_SIM_full_power_formula_and_default_planner_behavior(tmp_path):
    original, options = _setup(
        tmp_path,
        _context(
            geofence_valid=True,
            camera_available=True,
            perception_available=True,
        ),
    )
    derived, receipt = qualify_sim_planning_context(original, **options)
    model = DeterministicFlightModel(energy_profile=SimulationEnergyProfile(60, 10, 350, "fixture"))
    assert (
        model.energy_observation()["estimated_flight_time_s"] * 0.9
        == receipt.modeled_endurance_before_age_s
    )
    task = ResolvedTaskSpec(
        task_id="fixture-survey",
        task_type=TaskType.SURVEY,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="fixture-route",
            direction="northbound",
            length_m=12,
            buffer_m=2,
            centerline=(GeoPoint(1.3, 103.8), GeoPoint(1.3001, 103.8)),
            resolution_confidence=1.0,
        ),
        target_classes=("map_feature",),
        on_detection=(),
        constraints=TaskConstraints(40, 900, True),
        completion_action=PlanAction.RETURN,
    )
    policy = PlannerPolicy()
    estimate = estimate_task(task, derived, "multirotor", policy=policy)
    assert estimate.available_duration_s == pytest.approx(
        539.0 * (90 - 40 - policy.minimum_battery_margin_pct) / 90
    )


@pytest.mark.parametrize("receive_age_s,expected_available_s", [(1.0, 359.0), (1.9, 358.1)])
def test_scoped_SIM_budget_preserves_reserve_while_spending_full_receive_age(
    tmp_path, receive_age_s, expected_available_s
):
    original, options = _setup(
        tmp_path, _context(geofence_valid=True, camera_available=True, perception_available=False)
    )
    options["now_s"] = original.captured_at_s + receive_age_s
    derived, receipt = qualify_sim_planning_context(original, **options)
    task = ResolvedTaskSpec(
        task_id="fixture-reserve-accounting",
        task_type=TaskType.SURVEY,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="fixture-route",
            direction="northbound",
            length_m=12,
            buffer_m=2,
            centerline=(GeoPoint(1.3, 103.8), GeoPoint(1.3001, 103.8)),
            resolution_confidence=1.0,
        ),
        target_classes=("map_feature",),
        on_detection=(),
        constraints=TaskConstraints(25, 900, True),
        completion_action=PlanAction.RETURN,
    )
    requirements = RouteAcquisitionRequirements(
        task.canonical_hash,
        derived.canonical_hash,
        modeled_endurance_receive_age_s=receipt.conservative_receive_age_deduction_s,
    )
    estimate = estimate_task(task, derived, "multirotor", resource_requirements=requirements)
    # Independent physical bookkeeping: 60 Wh * 90%=54 Wh. Reserve+margin is
    # 30% of full capacity=18 Wh, leaving 36 Wh before elapsed receive time.
    # 360 W spends 0.1 Wh/s: 36 Wh/(0.1 Wh/s) - age = 360 s - age.
    remaining_energy_wh = 60.0 * 90.0 / 100
    reserved_energy_wh = 60.0 * (25.0 + PlannerPolicy().minimum_battery_margin_pct) / 100
    unreserved_endurance_s = (remaining_energy_wh - reserved_energy_wh) * 3600.0 / 360.0
    assert unreserved_endurance_s == 360.0
    assert estimate.available_duration_s == pytest.approx(expected_available_s, abs=1e-10)
    assert estimate.available_duration_s == pytest.approx(unreserved_endurance_s - receive_age_s)
    assert derived.battery_remaining_pct == original.battery_remaining_pct == 90.0
    assert derived.captured_at_s == original.captured_at_s == 10.0
    assert receipt.to_mapping()["reserve_deducted"] is False


def test_revalidation_recomputes_current_sources_without_renewing_timestamp(tmp_path):
    original, options = _setup(tmp_path, pointer="/payload")
    derived, receipt = qualify_sim_planning_context(original, **options)
    validate_sim_planning_qualification(
        receipt,
        observed_context=original,
        qualified_context=derived,
        runtime_attestation=options["runtime_attestation"],
        artifact_root=tmp_path,
        now_s=11.5,
        clock_id="fixture-clock",
    )
    assert derived.captured_at_s == 10.0
    with pytest.raises(AutonomyContractError, match="stale"):
        validate_sim_planning_qualification(
            receipt,
            observed_context=original,
            qualified_context=derived,
            runtime_attestation=options["runtime_attestation"],
            artifact_root=tmp_path,
            now_s=12.0001,
            clock_id="fixture-clock",
        )


@pytest.mark.parametrize(
    "name,value",
    [
        ("image", "fixture/px4@sha256:" + "4" * 64),
        ("scenario_id", "other-scenario"),
        ("scenario_manifest_sha256", "4" * 64),
        ("airframe_profile_id", "other-profile"),
        ("airframe_profile_hash", "4" * 64),
        ("runtime_kind", "REAL"),
        ("vehicle_class", "fixed-wing"),
        ("schema_version", 2),
        ("physical_calibration_claimed", True),
    ],
)
def test_model_scope_and_version_mismatch_denied(tmp_path, name, value):
    model = _model()
    model[name] = value
    original, options = _setup(tmp_path, model=model)
    with pytest.raises(AutonomyContractError):
        qualify_sim_planning_context(original, **options)


@pytest.mark.parametrize("runtime", ["HIL", "REAL", "REPLAY"])
def test_SIM_model_never_qualifies_other_runtime(tmp_path, runtime):
    context = _context(
        provenance=ContextProvenance(
            runtime,
            ContextValueSource("unknown"),
            ContextValueSource("unknown"),
            ContextValueSource("observed_wind_estimate", "b" * 64),
        )
    )
    original, options = _setup(tmp_path, context)
    with pytest.raises(AutonomyContractError, match="HIL/REAL/REPLAY"):
        qualify_sim_planning_context(original, **options)


@pytest.mark.parametrize(
    "section,name,value",
    [
        ("energy", "kind", "timeout_v1"),
        ("capability", "kind", "instantaneous_ground_speed_v1"),
        ("energy", "capacity_wh", 0),
        ("energy", "capacity_wh", float("nan")),
        ("energy", "avionics_power_w", -1),
        ("energy", "propulsion_max_power_w", float("inf")),
        ("energy", "reserve_deducted", True),
        ("energy", "reserve_deducted", 0),
        ("energy", "minimum_battery_pct", 100),
        ("energy", "maximum_battery_pct", 49),
        ("capability", "cruise_airspeed_mps", 0),
        ("capability", "cruise_airspeed_mps", True),
        ("capability", "maximum_wind_speed_mps", -1),
    ],
)
def test_invalid_or_unsupported_model_inputs_denied(tmp_path, section, name, value):
    model = _model()
    model[section][name] = value
    original, options = _setup(tmp_path, model=model)
    with pytest.raises(AutonomyContractError):
        qualify_sim_planning_context(original, **options)


@pytest.mark.parametrize(
    "overrides",
    [
        {"now_s": 12.00001},
        {"now_s": 9.99},
        {"attested_at_s": 8.99},
        {"attested_at_s": 11.01},
        {"maximum_age_s": 2.001},
        {"maximum_age_s": True},
        {"clock_id": "other-clock"},
    ],
)
def test_staleness_future_observation_clock_mismatch_and_age_relaxation_denied(tmp_path, overrides):
    original, options = _setup(tmp_path)
    options.update(overrides)
    with pytest.raises(AutonomyContractError):
        qualify_sim_planning_context(original, **options)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(context_sha256="e" * 64),
        lambda r: r["context"].update(battery_remaining_pct=91),
        lambda r: r["qualification_details"].update(qualified_plain_MC_disarmed_on_ground=False),
        lambda r: r["qualification_details"].update(source_file_bytes_SHA_verified=False),
        lambda r: r["qualification_details"].update(source_identity_before_after_equal=False),
        lambda r: r["qualification_details"].update(endurance_reserve_already_deducted=True),
        lambda r: r["qualification_details"].update(accepted_system_component=[True, 1]),
        lambda r: r["qualification_details"].update(message_received_at_s={}),
        lambda r: r["qualification_details"]["message_received_at_s"].update(HEARTBEAT=11.1),
        lambda r: r["qualification_details"].update(oldest_observation_at_s=10.01),
        lambda r: r["claims"].update(execution_authority=True),
        lambda r: r.update(source_references=[]),
    ],
)
def test_unqualified_or_differently_bound_observation_denied(tmp_path, mutation):
    original, options = _setup(tmp_path, observation_mutator=mutation)
    with pytest.raises(AutonomyContractError):
        qualify_sim_planning_context(original, **options)


@pytest.mark.parametrize("path", ["raw.json", "observation.json", "model.json"])
def test_changed_bound_source_denied_at_qualification_and_revalidation(tmp_path, path):
    original, options = _setup(tmp_path)
    derived, receipt = qualify_sim_planning_context(original, **options)
    (tmp_path / path).write_bytes(b"{}")
    with pytest.raises(AutonomyContractError, match="byte size|SHA"):
        qualify_sim_planning_context(original, **options)
    with pytest.raises(AutonomyContractError, match="byte size|SHA"):
        validate_sim_planning_qualification(
            receipt,
            observed_context=original,
            qualified_context=derived,
            runtime_attestation=options["runtime_attestation"],
            artifact_root=tmp_path,
            now_s=11.0,
            clock_id="fixture-clock",
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("estimated_flight_time_s", 500.0),
        ("qualified_context_sha256", "e" * 64),
        ("runtime_attestation_sha256", "e" * 64),
        ("scenario_id", "forged"),
        ("modeled_remaining_wh", 500.0),
        ("conservative_receive_age_deduction_s", 0.0),
    ],
)
def test_forged_qualification_receipt_denied(tmp_path, field, value):
    original, options = _setup(tmp_path)
    derived, receipt = qualify_sim_planning_context(original, **options)
    with pytest.raises(AutonomyContractError, match="recomputation differs"):
        validate_sim_planning_qualification(
            replace(receipt, **{field: value}),
            observed_context=original,
            qualified_context=derived,
            runtime_attestation=options["runtime_attestation"],
            artifact_root=tmp_path,
            now_s=11.0,
            clock_id="fixture-clock",
        )


def test_no_override_of_reported_endurance_or_existing_capability(tmp_path):
    original, options = _setup(tmp_path)
    derived, _ = qualify_sim_planning_context(original, **options)
    reported = replace(
        original,
        estimated_flight_time_s=500,
        provenance=replace(
            original.provenance, endurance=ContextValueSource("autopilot_reported", "c" * 64)
        ),
    )
    for context in (reported, replace(original, flight_capability=derived.flight_capability)):
        with pytest.raises(AutonomyContractError, match="overwrite"):
            qualify_sim_planning_context(context, **options)


def test_battery_and_wind_model_domains_are_enforced(tmp_path):
    for context in (_context(battery_remaining_pct=49.999), _context(wind_north_mps=2.001)):
        original, options = _setup(tmp_path, context)
        with pytest.raises(AutonomyContractError, match="supported domain"):
            qualify_sim_planning_context(original, **options)


def test_unknown_wind_is_not_fabricated(tmp_path):
    context = _context(
        wind_north_mps=None,
        wind_east_mps=None,
        provenance=replace(_context().provenance, wind=ContextValueSource("unknown")),
    )
    original, options = _setup(tmp_path, context)
    with pytest.raises(AutonomyContractError, match="calm-wind fabrication"):
        qualify_sim_planning_context(original, **options)


def test_zero_power_load_and_unknown_fields_and_duplicate_keys_denied(tmp_path):
    models = []
    model = _model()
    model["energy"].update(avionics_power_w=0, propulsion_max_power_w=0)
    models.append(model)
    model = _model()
    model["energy"]["maximum_runtime_s"] = 3600
    models.append(model)
    model = _model()
    model["arbitrary_authority"] = True
    models.append(model)
    for model in models:
        original, options = _setup(tmp_path, model=model)
        with pytest.raises(AutonomyContractError):
            qualify_sim_planning_context(original, **options)
    original, options = _setup(tmp_path)
    raw = b'{"contract":"a","contract":"b"}'
    (tmp_path / "model.json").write_bytes(raw)
    options["model_source"] = EvidenceBinding(
        "model.json", len(raw), hashlib.sha256(raw).hexdigest(), "duplicate-fixture", ""
    )
    with pytest.raises(AutonomyContractError, match="duplicate"):
        qualify_sim_planning_context(original, **options)


def test_directly_constructed_invalid_runtime_attestation_is_rechecked(tmp_path):
    original, options = _setup(tmp_path)
    for changed in (
        replace(options["runtime_attestation"], hardware_control_enabled=True),
        replace(options["runtime_attestation"], truth_free=False),
        replace(options["runtime_attestation"], telemetry_qualified=False),
        replace(options["runtime_attestation"], ownership_checks=(("bad", False),)),
    ):
        options["runtime_attestation"] = changed
        with pytest.raises(ValueError):
            qualify_sim_planning_context(original, **options)


def test_model_read_cap_and_missing_pointer_denied(tmp_path):
    original, options = _setup(tmp_path)
    for source in (
        replace(options["model_source"], bytes=16385),
        replace(options["model_source"], json_pointer=None),
        replace(options["model_source"], json_pointer="/missing"),
    ):
        options["model_source"] = source
        with pytest.raises(AutonomyContractError):
            qualify_sim_planning_context(original, **options)


def test_model_roundtrip_is_canonical_and_detached():
    raw = _model()
    model = ExplicitSimPlanningModel.from_mapping(raw)
    detached = model.to_mapping()
    changed = copy.deepcopy(detached)
    changed["energy"]["capacity_wh"] = 123
    assert model.to_mapping() == detached
    assert ExplicitSimPlanningModel.from_mapping(detached) == model
    assert ExplicitSimPlanningModel.from_mapping(changed) != model
