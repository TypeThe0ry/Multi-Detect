from __future__ import annotations

import copy
import json
import math
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from multidetect.autonomy.contracts import (
    AutonomyContractError,
    ConservativeFlightCapability,
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
from multidetect.autonomy.planner import PlannerError, PlannerPolicy, estimate_task, plan_task
from multidetect.autonomy.serialization import canonical_sha256, strict_json_loads


def _v1() -> ContextSnapshot:
    return ContextSnapshot(
        snapshot_id="source-context",
        captured_at_s=10.0,
        captured_at_utc="2026-10-08T00:00:00Z",
        aircraft_position=GeoPoint(1.3, 103.8),
        home_position=GeoPoint(1.3, 103.8),
        airframe_profile_id="explicit-multirotor",
        airframe_profile_hash="a" * 64,
        battery_remaining_pct=90.0,
        estimated_flight_time_s=3600.0,
        ground_speed_mps=0.0,
        airspeed_mps=0.0,
        wind_north_mps=0.0,
        wind_east_mps=0.0,
        gps_fix_type=3,
        gps_horizontal_accuracy_m=1.0,
        geofence_valid=True,
        camera_available=True,
        perception_available=True,
        map_cache_available=True,
        dem_available=True,
    )


def _capability(vehicle: str = "multirotor", runtime: str = "SIM") -> ConservativeFlightCapability:
    return ConservativeFlightCapability(
        "explicit-multirotor", "a" * 64, vehicle, runtime, 5.0, "b" * 64
    )


def _v2(vehicle: str = "multirotor", runtime: str = "SIM") -> ContextSnapshot:
    airspeed = ContextValueSource("unknown")
    measured = None
    if vehicle != "multirotor":
        airspeed = ContextValueSource("air_relative_sensor", "c" * 64)
        measured = 12.0
    return replace(
        _v1(),
        schema_version=2,
        airspeed_mps=measured,
        provenance=ContextProvenance(
            runtime,
            airspeed,
            ContextValueSource("autopilot_reported", "d" * 64),
            ContextValueSource("observed_wind_estimate", "e" * 64),
        ),
        flight_capability=_capability(vehicle, runtime),
    )


def _task() -> ResolvedTaskSpec:
    return ResolvedTaskSpec(
        task_id="bounded-task",
        task_type=TaskType.SURVEY,
        geometry=TaskGeometry(
            kind="map_feature",
            resolved_feature_id="north-route",
            direction="northbound",
            length_m=200.0,
            buffer_m=10.0,
            centerline=(GeoPoint(1.3, 103.8), GeoPoint(1.302, 103.8)),
            resolution_confidence=0.95,
        ),
        target_classes=("map_feature",),
        on_detection=(),
        constraints=TaskConstraints(40.0, 900.0, True),
        completion_action=PlanAction.RETURN,
    )


def test_v1_exact_mapping_and_canonical_identity_are_preserved() -> None:
    mapping = {
        "schema_version": 1,
        "snapshot_id": "source-context",
        "captured_at_s": 10.0,
        "captured_at_utc": "2026-10-08T00:00:00Z",
        "aircraft_position": {"latitude_deg": 1.3, "longitude_deg": 103.8},
        "home_position": {"latitude_deg": 1.3, "longitude_deg": 103.8},
        "airframe_profile_id": "explicit-multirotor",
        "airframe_profile_hash": "a" * 64,
        "battery_remaining_pct": 90.0,
        "estimated_flight_time_s": 3600.0,
        "ground_speed_mps": 0.0,
        "airspeed_mps": 0.0,
        "wind_north_mps": 0.0,
        "wind_east_mps": 0.0,
        "gps_fix_type": 3,
        "gps_horizontal_accuracy_m": 1.0,
        "geofence_valid": True,
        "camera_available": True,
        "perception_available": True,
        "map_cache_available": True,
        "dem_available": True,
    }
    assert _v1().to_mapping() == mapping
    assert _v1().canonical_hash == canonical_sha256(mapping)
    assert ContextSnapshot.from_mapping(mapping) == _v1()
    assert "provenance" not in mapping and "flight_capability" not in mapping
    unbound = replace(_v1(), airframe_profile_hash=None)
    assert "airframe_profile_hash" not in unbound.to_mapping()


@pytest.mark.parametrize("name", ["airspeed_mps", "estimated_flight_time_s", "wind_north_mps"])
def test_v1_never_accepts_missing_numbers(name: str) -> None:
    with pytest.raises(AutonomyContractError):
        replace(_v1(), **{name: None})


@pytest.mark.parametrize("field", ["provenance", "flight_capability"])
def test_v1_rejects_v2_fields_even_null_in_mapping(field: str) -> None:
    value = _v1().to_mapping()
    value[field] = None
    with pytest.raises(AutonomyContractError):
        ContextSnapshot.from_mapping(value)


def test_v2_unknown_roundtrip_is_explicit_and_immutable() -> None:
    context = replace(
        _v2(),
        estimated_flight_time_s=None,
        wind_north_mps=None,
        wind_east_mps=None,
        provenance=ContextProvenance(
            "SIM",
            ContextValueSource("unknown"),
            ContextValueSource("unknown"),
            ContextValueSource("unknown"),
        ),
        flight_capability=None,
    )
    value = context.to_mapping()
    assert value["airspeed_mps"] is None
    assert value["estimated_flight_time_s"] is None
    assert value["wind_north_mps"] is None and value["wind_east_mps"] is None
    assert value["flight_capability"] is None
    assert ContextSnapshot.from_mapping(strict_json_loads(context.canonical_json)) == context
    assert context.canonical_hash != _v1().canonical_hash
    value["provenance"]["airspeed"]["kind"] = "fabricated"
    assert context.provenance.airspeed.kind == "unknown"
    with pytest.raises(FrozenInstanceError):
        context.provenance.runtime_kind = "REAL"


@pytest.mark.parametrize("field", ["provenance", "flight_capability"])
def test_v2_mapping_requires_explicit_new_fields(field: str) -> None:
    value = _v2().to_mapping()
    del value[field]
    with pytest.raises(AutonomyContractError):
        ContextSnapshot.from_mapping(value)


@pytest.mark.parametrize("version", [True, 0, 3, "2", 2.0])
def test_only_exact_schema_versions_are_accepted(version: object) -> None:
    value = _v2().to_mapping()
    value["schema_version"] = version
    with pytest.raises(AutonomyContractError):
        ContextSnapshot.from_mapping(value)


@pytest.mark.parametrize("field", ["airspeed_mps", "estimated_flight_time_s", "wind_north_mps"])
@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "0"])
def test_v2_nonfinite_and_bool_are_not_missing_data(field: str, value: object) -> None:
    with pytest.raises(AutonomyContractError):
        replace(_v2(), **{field: value})


def test_unknown_cannot_be_filled_with_zero_or_known_source_set_to_null() -> None:
    with pytest.raises(AutonomyContractError, match="airspeed unknown"):
        replace(_v2(), airspeed_mps=0.0)
    with pytest.raises(AutonomyContractError, match="endurance unknown"):
        replace(_v2(), estimated_flight_time_s=None)
    with pytest.raises(AutonomyContractError, match="together"):
        replace(_v2(), wind_north_mps=None)


@pytest.mark.parametrize(
    ("kind", "digest"),
    [
        ("unknown", "f" * 64),
        ("autopilot_reported", None),
        ("autopilot_reported", "x" * 64),
        ("", None),
    ],
)
def test_source_identity_is_strict(kind: str, digest: str | None) -> None:
    with pytest.raises(AutonomyContractError):
        ContextValueSource(kind, digest)


@pytest.mark.parametrize("section", ["airspeed", "endurance", "wind"])
def test_source_namespace_and_extra_fields_rejected(section: str) -> None:
    value = _v2().to_mapping()
    value["provenance"][section] = {"kind": "ground_speed", "evidence_sha256": "f" * 64}
    with pytest.raises(AutonomyContractError):
        ContextSnapshot.from_mapping(value)
    value = _v2().to_mapping()
    value["provenance"][section]["qualified"] = True
    with pytest.raises(AutonomyContractError):
        ContextSnapshot.from_mapping(value)


@pytest.mark.parametrize("value", [0, -1, True, float("inf"), float("nan")])
def test_capability_speed_is_strict(value: object) -> None:
    with pytest.raises(AutonomyContractError):
        replace(_capability(), cruise_airspeed_mps=value)


def test_multirotor_missing_airspeed_can_plan_without_fabrication() -> None:
    context = _v2()
    estimate = estimate_task(_task(), context, "multirotor")
    assert context.airspeed_mps is None
    assert estimate.cruise_speed_mps == pytest.approx(5.0)
    assert plan_task(_task(), context, "multirotor").context_snapshot_hash == context.canonical_hash


def test_v2_groundspeed_and_instantaneous_airspeed_never_boost_capability() -> None:
    baseline = estimate_task(_task(), _v2(), "multirotor")
    faster = replace(
        _v2(),
        ground_speed_mps=200.0,
        airspeed_mps=100.0,
        provenance=replace(
            _v2().provenance, airspeed=ContextValueSource("air_relative_sensor", "c" * 64)
        ),
    )
    assert estimate_task(_task(), faster, "multirotor") == baseline
    legacy_slow = estimate_task(_task(), _v1(), "multirotor")
    legacy_fast = estimate_task(_task(), replace(_v1(), ground_speed_mps=20.0), "multirotor")
    assert legacy_fast.cruise_speed_mps > legacy_slow.cruise_speed_mps


@pytest.mark.parametrize("vehicle", ["fixed-wing", "vtol"])
def test_fixed_wing_cruise_needs_air_relative_source(vehicle: str) -> None:
    missing = replace(_v2(), flight_capability=_capability(vehicle))
    with pytest.raises(PlannerError, match="air-relative"):
        estimate_task(_task(), missing, vehicle)
    qualified = _v2(vehicle)
    assert estimate_task(_task(), qualified, vehicle).cruise_speed_mps == pytest.approx(5.0)
    estimated = replace(
        qualified,
        provenance=replace(
            qualified.provenance, airspeed=ContextValueSource("air_relative_estimator", "c" * 64)
        ),
    )
    assert estimate_task(_task(), estimated, vehicle) == estimate_task(_task(), qualified, vehicle)


@pytest.mark.parametrize("runtime", ["HIL", "REAL", "REPLAY"])
@pytest.mark.parametrize("section", ["endurance", "wind"])
def test_sim_models_cannot_cross_runtime(runtime: str, section: str) -> None:
    context = _v2(runtime=runtime)
    kind = "explicit_SIM_energy_model" if section == "endurance" else "explicit_SIM_wind_model"
    context = replace(
        context,
        provenance=replace(context.provenance, **{section: ContextValueSource(kind, "f" * 64)}),
    )
    assert ContextSnapshot.from_mapping(context.to_mapping()) == context
    with pytest.raises(PlannerError, match="SIM models"):
        estimate_task(_task(), context, "multirotor")


def test_sim_energy_and_wind_models_are_explicitly_usable_only_in_sim() -> None:
    context = _v2()
    context = replace(
        context,
        provenance=replace(
            context.provenance,
            endurance=ContextValueSource("explicit_SIM_energy_model", "f" * 64),
            wind=ContextValueSource("explicit_SIM_wind_model", "f" * 64),
        ),
    )
    assert math.isfinite(estimate_task(_task(), context, "multirotor").estimated_duration_s)


def test_unknown_endurance_is_recordable_but_no_energy_consuming_plan() -> None:
    context = _v2()
    context = replace(
        context,
        estimated_flight_time_s=None,
        provenance=replace(context.provenance, endurance=ContextValueSource("unknown")),
    )
    assert context.to_mapping()["estimated_flight_time_s"] is None
    with pytest.raises(PlannerError, match="flight time"):
        estimate_task(_task(), context, "multirotor")
    with pytest.raises(PlannerError, match="flight time"):
        plan_task(_task(), context, "multirotor")


def test_unknown_wind_and_absent_capability_fail_closed() -> None:
    context = _v2()
    unknown = replace(
        context,
        wind_north_mps=None,
        wind_east_mps=None,
        provenance=replace(context.provenance, wind=ContextValueSource("unknown")),
    )
    with pytest.raises(PlannerError, match="known wind"):
        estimate_task(_task(), unknown, "multirotor")
    with pytest.raises(PlannerError, match="conservative flight"):
        estimate_task(_task(), replace(context, flight_capability=None), "multirotor")


@pytest.mark.parametrize(
    "changes",
    [
        {"airframe_profile_id": "other"},
        {"airframe_profile_hash": "f" * 64},
        {"vehicle_class": "fixed-wing"},
        {"runtime_kind": "REAL"},
    ],
)
def test_capability_requires_exact_airframe_vehicle_runtime_binding(changes: dict) -> None:
    context = replace(_v2(), flight_capability=replace(_capability(), **changes))
    with pytest.raises(PlannerError, match="does not match"):
        estimate_task(_task(), context, "multirotor")


def test_v2_wind_model_energy_reserve_and_policy_bounds_remain_conservative() -> None:
    context = _v2()
    baseline = estimate_task(_task(), context, "multirotor")
    adverse = estimate_task(_task(), replace(context, wind_north_mps=2.0), "multirotor")
    assert adverse.estimated_duration_s > baseline.estimated_duration_s
    assert baseline.available_duration_s == pytest.approx(3600.0 * (90.0 - 40.0 - 5.0) / 90.0)
    capped = estimate_task(
        _task(), context, "multirotor", policy=PlannerPolicy(maximum_planning_speed_mps=3.0)
    )
    assert capped.cruise_speed_mps == pytest.approx(3.0)
    with pytest.raises(PlannerError, match="crosswind"):
        estimate_task(_task(), replace(context, wind_east_mps=6.0), "multirotor")
    with pytest.raises(PlannerError, match="energy budget"):
        estimate_task(_task(), replace(context, estimated_flight_time_s=1.0), "multirotor")


def test_new_documents_do_not_expand_other_schema_gates() -> None:
    from multidetect.autonomy.contracts import PromptEnvelope

    value = _v2().to_mapping()
    value["untrusted_extra"] = True
    with pytest.raises(AutonomyContractError):
        ContextSnapshot.from_mapping(value)
    request = {
        "schema_version": 2,
        "request_id": "r",
        "prompt_text_original": "p",
        "language": "en",
        "issued_at": "2026-10-08T00:00:00Z",
        "operator_id": "o",
        "aircraft_id": "a",
    }
    with pytest.raises(AutonomyContractError):
        PromptEnvelope.from_mapping(copy.deepcopy(request))


@pytest.mark.parametrize("runtime", ["SIM", "HIL", "REAL", "REPLAY"])
def test_autopilot_endurance_and_observed_wind_preserve_explicit_runtime(runtime: str) -> None:
    context = _v2(runtime=runtime)
    assert estimate_task(_task(), context, "multirotor").available_duration_s == 1800.0
    assert context.provenance.endurance.kind == "autopilot_reported"


def test_v1_constructor_cannot_accept_v2_objects() -> None:
    with pytest.raises(AutonomyContractError, match="v1 context"):
        replace(_v1(), provenance=_v2().provenance)
    with pytest.raises(AutonomyContractError, match="v1 context"):
        replace(_v1(), flight_capability=_capability())


@pytest.mark.parametrize("runtime", ["sim", "FLIGHT", True, 1])
def test_runtime_kind_is_exact(runtime: object) -> None:
    with pytest.raises(AutonomyContractError):
        replace(_v2().provenance, runtime_kind=runtime)


def test_missing_airframe_hash_cannot_qualify_capability() -> None:
    with pytest.raises(PlannerError, match="does not match"):
        estimate_task(_task(), replace(_v2(), airframe_profile_hash=None), "multirotor")


def test_v2_uses_explicit_speed_below_nominal_policy_not_legacy_floor() -> None:
    context = replace(_v2(), flight_capability=replace(_capability(), cruise_airspeed_mps=2.0))
    assert estimate_task(_task(), context, "multirotor").cruise_speed_mps == pytest.approx(2.0)
    context = replace(context, flight_capability=replace(_capability(), cruise_airspeed_mps=1.0))
    with pytest.raises(PlannerError, match="progress"):
        estimate_task(_task(), context, "multirotor")


def _schema(version: int) -> Draft202012Validator:
    name = (
        "autonomy-context-snapshot-v2.schema.json"
        if version == 2
        else "autonomy-context-snapshot.schema.json"
    )
    path = Path(__file__).resolve().parents[1] / "configs" / "schemas" / name
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_separate_v2_schema_and_v1_schema_never_admit_each_other() -> None:
    v1 = _schema(1)
    v2 = _schema(2)
    v1.validate(_v1().to_mapping())
    v2.validate(_v2().to_mapping())
    assert not v1.is_valid(_v2().to_mapping())
    assert not v2.is_valid(_v1().to_mapping())
    unknown = replace(
        _v2(),
        estimated_flight_time_s=None,
        wind_north_mps=None,
        wind_east_mps=None,
        provenance=ContextProvenance(
            "SIM",
            ContextValueSource("unknown"),
            ContextValueSource("unknown"),
            ContextValueSource("unknown"),
        ),
        flight_capability=None,
    )
    v2.validate(unknown.to_mapping())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), 1),
        (("airspeed_mps",), 0.0),
        (("airspeed_mps",), True),
        (("estimated_flight_time_s",), None),
        (("wind_north_mps",), None),
        (("gps_fix_type",), 9),
        (("ground_speed_mps",), None),
        (("provenance",), None),
        (("provenance", "airspeed", "kind"), "ground_speed"),
        (("provenance", "airspeed", "evidence_sha256"), "f" * 64),
        (("provenance", "endurance", "evidence_sha256"), None),
        (("provenance", "endurance", "extra"), True),
        (("flight_capability", "cruise_airspeed_mps"), 0),
        (("flight_capability", "cruise_airspeed_mps"), True),
        (("flight_capability", "model_sha256"), "x" * 64),
        (("flight_capability", "runtime_kind"), "sim"),
        (("flight_capability", "vehicle_class"), "quad"),
        (("flight_capability", "extra"), True),
        (("aircraft_position", "altitude_m"), 30.0),
        (("untrusted_extra",), True),
    ],
)
def test_v2_schema_and_application_reject_semantic_tampering(
    path: tuple[str, ...], value: object
) -> None:
    document = _v2().to_mapping()
    destination = document
    for key in path[:-1]:
        destination = destination[key]
    destination[path[-1]] = value
    assert not _schema(2).is_valid(document)
    with pytest.raises(AutonomyContractError):
        ContextSnapshot.from_mapping(document)


def test_schema_is_structural_not_runtime_or_source_authentication() -> None:
    context = _v2(runtime="REAL")
    context = replace(
        context,
        provenance=replace(
            context.provenance, endurance=ContextValueSource("explicit_SIM_energy_model", "f" * 64)
        ),
    )
    _schema(2).validate(context.to_mapping())
    with pytest.raises(PlannerError, match="SIM models"):
        estimate_task(_task(), context, "multirotor")
