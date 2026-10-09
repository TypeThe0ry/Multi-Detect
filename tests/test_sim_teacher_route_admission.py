"""Full original-wire CPU/source orchestration; not live SIM or flight proof."""

from __future__ import annotations

import copy
import json
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest
from test_sim_airframe_envelope import _binding, _write_reply
from test_sim_airframe_envelope import _fixture as airframe_fixture
from test_sim_navigation_reference import binding, native_binding
from test_sim_planning_inputs import _model
from test_sim_teacher_environment import GROUND
from test_sim_teacher_static_admission import json_source
from test_teacher_live_navigation_checkpoint import live_fixture as raw_live_fixture

from multidetect.autonomy import sim_teacher_route_admission as api
from multidetect.autonomy.contracts import (
    GeoPoint,
    MissionCommand,
    PlanAction,
    ResolvedTaskSpec,
    TaskConstraints,
    TaskGeometry,
    TaskType,
)
from multidetect.autonomy.onboard_teacher_mission import HeldTeacherLeg
from multidetect.autonomy.serialization import AutonomyContractError, canonical_json
from multidetect.autonomy.sim_airframe_envelope import PINNED_IMAGE, decode_generated_sdf_reply
from multidetect.autonomy.sim_teacher_environment import load_static_teacher_environment
from multidetect.autonomy.teacher_live_navigation_checkpoint import (
    qualify_teacher_live_navigation_checkpoint,
)
from multidetect.px4_mission import MultirotorMissionGeometry, Px4MultirotorSafetyPolicy
from multidetect.target_geolocation import wgs84_forward_from_ned

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def inputs(tmp_path, request):
    generator = raw_live_fixture.__wrapped__(tmp_path)
    args, capture, clock, _ = next(generator)
    try:
        model_inputs = airframe_fixture(tmp_path)
        loaded = ET.fromstring(decode_generated_sdf_reply((tmp_path / "reply.bin").read_bytes()))
        vehicle = next(
            m for m in loaded[0].findall("model") if m.attrib["name"] == "x500_mono_cam_0"
        )
        vehicle.find("pose").text = "0 0 0 0 0 0"
        pad = next(
            m for m in loaded[0].findall("model") if m.attrib["name"] == "multirotor_home_pad"
        )
        ET.SubElement(pad, "static").text = "true"
        pad.find("pose").text = "0 0 .0065 0 0 0"
        pad.find("link/collision/geometry/cylinder/length").text = ".013"
        source_world = ET.fromstring((tmp_path / "world.sdf").read_bytes())
        source_world[0].append(copy.deepcopy(pad))
        source_world[0].append(ET.fromstring(GROUND))
        source_world[0].append(copy.deepcopy(loaded[0].find("physics")))
        if getattr(request, "param", None) in {"building", "foreign_platform"}:
            high = request.param == "building"
            pose, size = (
                ("1.5 0 12.24 0 0 0", ".2 .2 .5") if high else ("0 0 .002 0 0 0", ".1 .1 .004")
            )
            source_world[0].append(
                ET.fromstring(
                    f'<model name="CPU_obstacle"><static>true</static><pose>{pose}</pose>'
                    f'<link name="body"><collision name="collision"><geometry><box>'
                    f"<size>{size}</size></box></geometry></collision></link></model>"
                )
            )
        (tmp_path / "world.sdf").write_bytes(ET.tostring(source_world))
        args["world_source"] = binding(tmp_path, "world.sdf")
        args["model_sources"] = model_inputs["model_sources"]
        generated = copy.deepcopy(source_world)
        generated.attrib["version"] = "1.10"
        generated[0].append(copy.deepcopy(vehicle))
        if getattr(request, "param", None) == "bad_ground":
            generated[0].find("model[@name='x500_mono_cam_0']/pose").text = "0 0 -.04 0 0 0"
        _write_reply(tmp_path, generated)
        image = (
            PINNED_IMAGE
            if getattr(request, "param", None) != "wrong_image"
            else "other/image@sha256:" + "7" * 64
        )
        args["plan"]["docker_plan"]["image"] = image
        for role in ("before", "after"):
            row = args[f"owned_inspect_{role}"]
            stream = row["streams"]["stdout"]
            path = Path(stream["original_bytes"]["path"])
            value = json.loads(path.read_bytes())
            value[0]["Config"]["Image"] = image
            path.write_text(json.dumps(value), encoding="utf-8")
            source = binding(tmp_path, path.relative_to(tmp_path).as_posix())
            stream.update(
                original_bytes=native_binding(tmp_path, source),
                observed_bytes=source.bytes,
                full_output_sha256=source.sha256,
            )
        if getattr(request, "param", None) == "one_camera":
            args["camera_updates"] = args["camera_updates"][:1]
        checkpoint = qualify_teacher_live_navigation_checkpoint(**args)
        environment = load_static_teacher_environment(
            artifact_root=tmp_path,
            world_source=args["world_source"],
            generated_sdf_source=_binding(tmp_path, "reply.bin"),
            include_model_sources={},
        )
        home = checkpoint.observed_context.home_position
        points = []
        for east, north in ((3, 0), (3, 3), (0, 0)):
            lat, lon = wgs84_forward_from_ned(
                latitude_deg=home.latitude_deg,
                longitude_deg=home.longitude_deg,
                north_offset_m=north,
                east_offset_m=east,
            )
            points.append(GeoPoint(lat, lon, 12, "relative_home"))
        geometry = MultirotorMissionGeometry(
            home=home,
            waypoints=tuple(points),
            takeoff_altitude_m=12,
            loiter_time_s=2,
            loiter_radius_m=0.5,
            acceptance_radius_m=0.5,
        )
        task = ResolvedTaskSpec(
            task_id="CPU-source-route",
            task_type=TaskType.SURVEY,
            geometry=TaskGeometry(
                kind="polyline",
                resolved_feature_id="CPU-static-route",
                direction="forward",
                length_m=10,
                centerline=tuple(points),
                resolution_confidence=1,
            ),
            target_classes=("route-collection",),
            on_detection=(),
            constraints=TaskConstraints(25, 900, True),
            completion_action=PlanAction.RETURN,
        )
        map_doc = json.loads(
            (ROOT / "simulation/maps/urban_inspection_multirotor_001.geojson").read_bytes()
        )
        dem_doc = json.loads(
            (ROOT / "simulation/terrain/urban_inspection_multirotor_001.dem.json").read_bytes()
        )
        map_doc["scenario_id"] = dem_doc["scenario_id"] = "fixture-scenario"
        model = _model()
        model["image"] = image
        # This deliberately roomy CPU-fixture declaration is not the actual
        # vehicle battery. A separate negative preserves the original 60Wh.
        model["energy"]["capacity_wh"] = 120
        model["airframe_profile_hash"] = checkpoint.observed_context.airframe_profile_hash
        yield (
            dict(
                checkpoint=checkpoint,
                task=task,
                model_source=json_source(tmp_path, "model.json", model),
                artifact_root=tmp_path,
                environment=environment,
                map_source=json_source(tmp_path, "map.json", map_doc),
                dem_source=json_source(tmp_path, "dem.json", dem_doc),
                extraction_source=model_inputs["extraction_source"],
                geometry=geometry,
                safety_policy=Px4MultirotorSafetyPolicy(
                    geofence_center=home,
                    geofence_radius_m=15,
                    allowed_airframe_profile_ids=(checkpoint.observed_context.airframe_profile_id,),
                    maximum_relative_altitude_m=20,
                    maximum_mission_radius_m=10,
                    maximum_loiter_radius_m=1,
                ),
                legs=tuple(HeldTeacherLeg(i + 1, 2, yaw) for i, yaw in enumerate((0, 90, 0))),
                now_s=clock.value,
                clock_id=clock.session_id,
            ),
            capture,
            clock,
        )
    finally:
        generator.close()


def route_options(args):
    return {k: v for k, v in args.items() if k not in {"task", "model_source"}}


def test_source_assets_qualify_resource_flags_and_actual_shared_draft_without_perception(inputs):
    args, capture, _ = inputs
    original = args["checkpoint"].observed_context.to_mapping()
    draft, admission = api.compile_source_bound_static_teacher_mission(**args)
    row = admission.to_mapping()
    assert (
        draft.context.map_cache_available
        and draft.context.dem_available
        and draft.context.geofence_valid
    )
    assert draft.context.perception_available is False
    assert draft.context.airspeed_mps is None
    assert draft.context.camera_available is True
    assert row["original_observed_context"] == original
    assert args["checkpoint"].observed_context.to_mapping() == original
    assert not any(original[k] for k in ("map_cache_available", "dem_available", "geofence_valid"))
    assert draft.context.captured_at_s == original["captured_at_s"]
    assert row["static_design_clear"] is True and row["execution_ready"] is False
    assert not any(row["claims"].values())
    assert not any(draft.to_mapping()["claims"].values())
    assert draft.context_admission.planning_qualification_sha256 == admission.canonical_hash
    assert (
        draft.context_admission.runtime_attestation_sha256
        == args["checkpoint"].runtime_attestation.canonical_hash
    )
    assert tuple(x.command for x in draft.compiled_mission.executable_mission.items) == (
        MissionCommand.TAKEOFF,
        MissionCommand.WAYPOINT,
        MissionCommand.WAYPOINT,
        MissionCommand.WAYPOINT,
        MissionCommand.LOITER_TIME,
        MissionCommand.LAND,
    )
    assert row["airborne_envelope"]["attitude_domain"]["maximum_body_up_tilt_deg"] == 180
    assert row["near_ground_envelope"]["attitude_domain"]["maximum_body_up_tilt_deg"] == 1
    assert row["ground_geometry"]["source_derived_all_collision_bottom_up_m"] == pytest.approx(
        0.013
    )
    assert row["ground_geometry"]["source_derived_all_collision_bottom_up_m"] != pytest.approx(
        0.24 - row["airborne_envelope"]["envelope_bounds"]["horizontal_radius_m"]
    )
    phases = {x["phase"] for x in row["audited_intervals"]}
    assert phases == {
        "contact_departure",
        "takeoff",
        "transit-1",
        "transit-2",
        "transit-3",
        "hold-1",
        "hold-2",
        "hold-3",
        "terminal_loiter",
        "landing_approach",
        "contact_landing",
    }
    assert not capture._closed


@pytest.mark.parametrize(
    "change", ["clock", "expired", "leg", "home", "return", "LAND", "too_low", "fence"]
)
def test_unknown_stale_wrong_scope_or_incomplete_geometry_denies_before_authoring(inputs, change):
    args, _, _ = inputs
    if change == "clock":
        args["clock_id"] = "other"
    elif change == "expired":
        args["now_s"] = args["checkpoint"].expires_at_s
    elif change == "leg":
        args["legs"] = args["legs"][:-1]
    elif change == "home":
        args["geometry"] = replace(args["geometry"], home=GeoPoint(1.3522, 103.8198))
    elif change == "return":
        args["geometry"] = replace(
            args["geometry"],
            waypoints=args["geometry"].waypoints[:-1] + (args["geometry"].waypoints[0],),
        )
    elif change == "LAND":
        args["geometry"] = replace(
            args["geometry"], landing=GeoPoint(1.3521, 103.8198, 0.1, "relative_home")
        )
    elif change == "too_low":
        args["geometry"] = replace(args["geometry"], takeoff_altitude_m=1)
    elif change == "fence":
        args["safety_policy"] = replace(
            args["safety_policy"], geofence_radius_m=5, maximum_mission_radius_m=5
        )
    with pytest.raises(ValueError):
        api.compile_source_bound_static_teacher_mission(**args)


def test_forged_checkpoint_flags_cannot_replace_source_recomputation(inputs):
    args, _, _ = inputs
    row = args["checkpoint"].to_mapping()
    row["origin"]["verification"]["EKF_reset_continuity_verified"] = True
    row["observed_context_receipt"]["context"]["perception_available"] = True
    args["checkpoint"] = replace(args["checkpoint"], _document_json=canonical_json(row))
    with pytest.raises(AutonomyContractError, match="source/clock"):
        api.compile_source_bound_static_teacher_mission(**args)


@pytest.mark.parametrize(
    "field,value",
    [
        ("gps_accuracy_multiplier", 0),
        ("ground_tilt_domain_deg", 2),
        ("ground_tilt_domain_deg", True),
        ("projection_margin_m", float("inf")),
    ],
)
def test_ground_and_margin_policy_is_explicit_and_bounded(field, value):
    with pytest.raises(AutonomyContractError):
        api.StaticTeacherRoutePolicy(**{field: value})


@pytest.mark.parametrize(
    "path", ["map.json", "dem.json", "world.sdf", "x500_base.sdf", "reply.bin", "rgb-0.bin"]
)
def test_source_drift_cannot_produce_a_draft(inputs, path):
    args, _, _ = inputs
    (args["artifact_root"] / path).write_bytes(b"changed")
    with pytest.raises(ValueError):
        api.compile_source_bound_static_teacher_mission(**args)


@pytest.mark.parametrize("inputs", ["building"], indirect=True)
def test_static_building_midsegment_hold_and_landing_all_audited(inputs):
    args, _, _ = inputs
    result = api.audit_source_bound_static_teacher_route(**route_options(args))
    assert result.static_design_clear is False
    assert any(
        hit["collision_id"] == "CPU_obstacle/body/collision"
        for hit in result.to_mapping()["collision_hits"]
    )
    with pytest.raises(AutonomyContractError, match="intersects"):
        api.compile_source_bound_static_teacher_mission(**args)


def test_no_ground_blanket_exemption_for_a_small_foreign_platform(inputs, monkeypatch):
    args, _, _ = inputs
    original = api._segment_hits_box
    calls = []

    def check(a, b, minimum, maximum):
        calls.append((a, b, minimum, maximum))
        return original(a, b, minimum, maximum)

    monkeypatch.setattr(api, "_segment_hits_box", check)
    result = api.audit_source_bound_static_teacher_route(**route_options(args))
    exceptions = result.to_mapping()["contact_phase_exceptions"]
    assert all(x["phase"] in {"contact_departure", "contact_landing"} for x in exceptions)
    assert all(
        x["collision_id"] in {"ground/base/ground", "multirotor_home_pad/pad_link/pad_collision"}
        for x in exceptions
    )
    assert calls  # even allowed ground/support remain obstacles in airborne phases


@pytest.mark.parametrize("inputs", ["foreign_platform"], indirect=True)
def test_small_below_support_obstacle_is_not_exempt_without_full_footprint(inputs):
    args, _, _ = inputs
    result = api.audit_source_bound_static_teacher_route(**route_options(args))
    row = result.to_mapping()
    assert not result.static_design_clear
    assert {
        x["phase"]
        for x in row["collision_hits"]
        if x["collision_id"] == "CPU_obstacle/body/collision"
    } == {"contact_departure", "contact_landing"}
    assert not any(
        x["collision_id"] == "CPU_obstacle/body/collision" for x in row["contact_phase_exceptions"]
    )


def test_task_with_perception_branch_and_changed_centerline_is_not_collector(inputs):
    args, _, _ = inputs
    args["task"] = replace(args["task"], on_detection=(PlanAction.RETURN,))
    with pytest.raises(AutonomyContractError, match="non-perception"):
        api.compile_source_bound_static_teacher_mission(**args)


@pytest.mark.parametrize(
    "inputs,reason",
    [
        ("one_camera", "two current"),
        ("bad_ground", "signed ground"),
        ("wrong_image", "runtime image"),
    ],
    indirect=["inputs"],
)
def test_missing_settled_window_bad_contact_and_wrong_runtime_image_fail_closed(inputs, reason):
    args, _, _ = inputs
    with pytest.raises(AutonomyContractError, match=reason):
        api.compile_source_bound_static_teacher_mission(**args)


def test_shared_energy_reserve_is_not_relaxed_to_force_a_route_pass(inputs):
    args, _, _ = inputs
    value = json.loads((args["artifact_root"] / "model.json").read_bytes())
    value["energy"]["capacity_wh"] = 60
    args["model_source"] = json_source(args["artifact_root"], "model.json", value)
    with pytest.raises(ValueError, match="supplemental duration"):
        api.compile_source_bound_static_teacher_mission(**args)


def test_late_source_change_after_compiler_return_is_not_admitted(inputs, monkeypatch):
    args, _, _ = inputs
    original = api.compile_teacher_held_mission

    def mutate(**kwargs):
        result = original(**kwargs)
        (args["artifact_root"] / "map.json").write_bytes(b"{}")
        return result

    monkeypatch.setattr(api, "compile_teacher_held_mission", mutate)
    with pytest.raises(AutonomyContractError, match="SHA"):
        api.compile_source_bound_static_teacher_mission(**args)


def test_historical_checkpoint_exact_time_replay_is_not_current_authoring(inputs):
    args, capture, _ = inputs
    capture.abort("CPU test closes original live owner")
    with pytest.raises(RuntimeError, match="closed or poisoned"):
        api.compile_source_bound_static_teacher_mission(**args)


def test_static_processing_time_is_charged_before_model_budget_qualification(inputs, monkeypatch):
    args, _, clock = inputs
    original = api.audit_source_bound_static_teacher_route

    def slow(**kwargs):
        result = original(**kwargs)
        clock.value += 0.4
        return result

    monkeypatch.setattr(api, "audit_source_bound_static_teacher_route", slow)
    draft, row = api.compile_source_bound_static_teacher_mission(**args)
    value = row.to_mapping()
    assert value["qualified_at_s"] == pytest.approx(args["now_s"] + 0.4)
    assert value["static_audited_at_s"] == args["now_s"]
    assert value["energy_qualification"]["conservative_receive_age_deduction_s"] == pytest.approx(
        args["now_s"] + 0.4 - draft.context.captured_at_s
    )
    assert draft.context_admission.qualified_at_s == value["qualified_at_s"]
    assert draft.context_admission.expires_at_s == args["checkpoint"].expires_at_s


def test_final_compile_delay_expiry_cannot_return_provisional_artifact(inputs, monkeypatch):
    args, _, clock = inputs
    original = api.compile_teacher_held_mission

    def slow(**kwargs):
        result = original(**kwargs)
        clock.value = args["checkpoint"].expires_at_s
        return result

    monkeypatch.setattr(api, "compile_teacher_held_mission", slow)
    with pytest.raises(AutonomyContractError, match="expired"):
        api.compile_source_bound_static_teacher_mission(**args)


def test_admission_recomputation_rejects_forged_bool_int_and_receipt_extra_fields(inputs):
    args, _, _ = inputs
    result = api.audit_source_bound_static_teacher_route(**route_options(args))
    row = result.to_mapping()
    row["static_design_clear"] = 1
    with pytest.raises(AutonomyContractError, match="recomputation"):
        api.validate_source_bound_static_teacher_route(
            api.StaticTeacherRouteAdmission(canonical_json(row)), **route_options(args)
        )
