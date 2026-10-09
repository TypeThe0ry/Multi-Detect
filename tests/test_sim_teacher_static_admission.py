"""Strict source/coverage CPU fixtures; no live resource or flight claims."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_sim_teacher_environment import _environment

from multidetect.autonomy import sim_teacher_static_admission as api
from multidetect.autonomy.contracts import GeoPoint
from multidetect.autonomy.onboard_executed_trajectory import EvidenceBinding
from multidetect.autonomy.serialization import AutonomyContractError
from multidetect.px4_mission import MultirotorMissionGeometry, Px4MultirotorSafetyPolicy
from multidetect.target_geolocation import wgs84_forward_from_ned

ROOT = Path(__file__).resolve().parents[1]


def json_source(root, path, value):
    raw = json.dumps(value).encode()
    (root / path).write_bytes(raw)
    return EvidenceBinding(path, len(raw), hashlib.sha256(raw).hexdigest(), "CPU-only-resource", "")


def resource_inputs(root):
    environment = _environment(root)
    map_doc = json.loads(
        (ROOT / "simulation/maps/urban_inspection_multirotor_001.geojson").read_bytes()
    )
    dem_doc = json.loads(
        (ROOT / "simulation/terrain/urban_inspection_multirotor_001.dem.json").read_bytes()
    )
    home = GeoPoint(1.3521, 103.8198)
    lat, lon = wgs84_forward_from_ned(
        latitude_deg=home.latitude_deg,
        longitude_deg=home.longitude_deg,
        north_offset_m=3,
        east_offset_m=3,
    )
    geometry = MultirotorMissionGeometry(
        home=home,
        waypoints=(
            GeoPoint(lat, lon, 12, "relative_home"),
            GeoPoint(home.latitude_deg, home.longitude_deg, 12, "relative_home"),
        ),
        takeoff_altitude_m=12,
        loiter_time_s=2,
        loiter_radius_m=0.5,
        acceptance_radius_m=0.5,
    )
    policy = Px4MultirotorSafetyPolicy(
        geofence_center=home,
        geofence_radius_m=15,
        allowed_airframe_profile_ids=("px4_multirotor_gazebo_v1",),
        maximum_mission_radius_m=10,
        maximum_loiter_radius_m=1,
        maximum_relative_altitude_m=20,
    )
    return dict(
        artifact_root=root,
        environment=environment,
        map_source=json_source(root, "map.json", map_doc),
        dem_source=json_source(root, "dem.json", dem_doc),
        scenario_id=map_doc["scenario_id"],
        geometry=geometry,
        safety_policy=policy,
        horizontal_tube_m=2,
        vertical_tracking_uncertainty_m=0.2,
    )


def test_real_small_map_dem_schema_full_convex_coverage_and_uncertainty(tmp_path):
    args = resource_inputs(tmp_path)
    result = api.audit_static_teacher_resources(**args)
    row = result.to_mapping()
    assert row["vertical_clearance_margin_m"] == pytest.approx(0.2 + 3 * 0.4 + 2 * 0.002)
    assert row["qualified_resources"] == dict(
        map_cache_available=True, dem_available=True, geofence_valid=True
    )
    assert row["finite_base_ground_collision_ids"] == ["ground/base/ground"]
    assert row["claims"]["perception_available"] is False
    assert not any(row["claims"].values())
    api.validate_static_teacher_resources(result, **args)
    row["qualified_resources"]["dem_available"] = False
    assert result.to_mapping()["qualified_resources"]["dem_available"] is True


@pytest.mark.parametrize(
    "which,mutate",
    [
        ("map", lambda x: x.update(schema_version=True)),
        ("map", lambda x: x.update(coordinate_reference="ENU")),
        ("map", lambda x: x.update(scenario_id="other")),
        ("map", lambda x: x.update(extra=True)),
        ("map", lambda x: x["features"].append(copy.deepcopy(x["features"][0]))),
        ("map", lambda x: x["features"][0]["geometry"].update(type="MultiPolygon")),
        ("map", lambda x: x["features"][0]["geometry"]["coordinates"].append([])),
        ("map", lambda x: x["features"][0]["geometry"]["coordinates"][0][1].__setitem__(0, 103.82)),
        ("map", lambda x: x["features"][0]["geometry"]["coordinates"][0][0].__setitem__(0, True)),
        ("map", lambda x: x["features"][0]["geometry"]["coordinates"][0].__setitem__(-1, [0, 0])),
        ("map", lambda x: x["features"][0]["properties"].update(role="perception_map")),
        ("dem", lambda x: x.update(vertical_datum="ellipsoid")),
        ("dem", lambda x: x.update(vertical_sigma_m=0)),
        ("dem", lambda x: x.update(scenario_id="other")),
        ("dem", lambda x: x["origin"].update(latitude_deg=1.3522)),
        ("dem", lambda x: x["elevation_m"][0].__setitem__(0, 19)),
    ],
)
def test_schema_frame_datum_shape_unknown_values_fail_closed(tmp_path, which, mutate):
    args = resource_inputs(tmp_path)
    source = args[f"{which}_source"]
    value = json.loads((tmp_path / source.path).read_bytes())
    mutate(value)
    args[f"{which}_source"] = json_source(tmp_path, source.path, value)
    with pytest.raises(ValueError):
        api.audit_static_teacher_resources(**args)


def test_closed_map_bbox_is_not_a_polygon_coverage_proof(tmp_path):
    args = resource_inputs(tmp_path)
    value = json.loads((tmp_path / "map.json").read_bytes())
    value["features"][0]["geometry"]["coordinates"][0] = [
        [103.8158, 1.3481],
        [103.8238, 1.3481],
        [103.8198, 1.3561],
        [103.8158, 1.3561],
        [103.8158, 1.3481],
    ]
    args["map_source"] = json_source(tmp_path, "map.json", value)
    with pytest.raises(AutonomyContractError, match="rectangle"):
        api.audit_static_teacher_resources(**args)


@pytest.mark.parametrize("kind", ["map", "DEM", "plane", "route"])
def test_full_fence_uncertainty_and_route_coverage_not_just_origin(tmp_path, kind):
    args = resource_inputs(tmp_path)
    if kind == "map":
        value = json.loads((tmp_path / "map.json").read_bytes())
        value["features"][0]["geometry"]["coordinates"] = [
            [
                [103.81979, 1.35209],
                [103.81981, 1.35209],
                [103.81981, 1.35211],
                [103.81979, 1.35211],
                [103.81979, 1.35209],
            ]
        ]
        args["map_source"] = json_source(tmp_path, "map.json", value)
    elif kind == "DEM":
        value = json.loads((tmp_path / "dem.json").read_bytes())
        value.update(north_origin_m=-2, east_origin_m=-2, north_spacing_m=1, east_spacing_m=1)
        args["dem_source"] = json_source(tmp_path, "dem.json", value)
    elif kind == "plane":
        args["safety_policy"] = replace(args["safety_policy"], geofence_radius_m=60)
    else:
        args["horizontal_tube_m"] = 14
    with pytest.raises(AutonomyContractError, match="coverage|plane|route tube"):
        api.audit_static_teacher_resources(**args)


@pytest.mark.parametrize(
    "field,value",
    [
        ("horizontal_tube_m", True),
        ("vertical_tracking_uncertainty_m", float("nan")),
        ("dem_sigma_multiplier", 1),
    ],
)
def test_margin_domain_is_explicit_not_bool_or_nonfinite(tmp_path, field, value):
    args = resource_inputs(tmp_path)
    args[field] = value
    with pytest.raises(AutonomyContractError):
        api.audit_static_teacher_resources(**args)


def test_source_bytes_and_late_source_drift_prevent_admission(tmp_path, monkeypatch):
    args = resource_inputs(tmp_path)
    args["map_source"] = replace(args["map_source"], sha256="f" * 64)
    with pytest.raises(AutonomyContractError, match="SHA"):
        api.audit_static_teacher_resources(**args)
    args = resource_inputs(tmp_path)
    original = api._environment
    calls = [0]

    def change(root, declared):
        calls[0] += 1
        result = original(root, declared)
        if calls[0] == 2:
            (root / "dem.json").write_bytes(b"{}")
        return result

    monkeypatch.setattr(api, "_environment", change)
    with pytest.raises(AutonomyContractError, match="SHA"):
        api.audit_static_teacher_resources(**args)


def test_forged_receipt_bool_int_and_extra_field_are_recomputed(tmp_path):
    args = resource_inputs(tmp_path)
    row = api.audit_static_teacher_resources(**args).to_mapping()
    row["qualified_resources"]["dem_available"] = 1
    with pytest.raises(AutonomyContractError, match="recomputation"):
        api.validate_static_teacher_resources(
            api.StaticTeacherResourceAudit(json.dumps(row)), **args
        )
