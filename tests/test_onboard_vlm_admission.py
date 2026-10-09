from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest
from test_onboard_realtime_bridge import _bridge, _context
from test_onboard_vlm_provider import _Clock, _FakeWorker, _source

from multidetect.autonomy.contracts import GeoPoint
from multidetect.autonomy.onboard_agent import OnboardEventKind, OnboardRuntimeEvent
from multidetect.autonomy.onboard_vlm_admission import (
    ExecutionAdmissionPolicy,
    ExecutionSnapshot,
    OnboardVlmAdmissionError,
    RouteClearanceEvidence,
    SourceAnchoredIntent,
    admit_anchored_intent,
    compile_anchored_intent_at_current,
    minimum_compiled_path_length_m,
    wgs84_inverse_offsets,
)
from multidetect.autonomy.serialization import canonical_json
from multidetect.target_geolocation import wgs84_forward_from_ned

POLICY = ExecutionAdmissionPolicy(trusted_clearance_source_ids=("fixture-segment-clearance",))


def _clearance(context, event, bound_target, *, wire_hash="f" * 64, **changes):
    claims = {
        "contract": "multidetect.host-route-clearance-development-v1",
        "context_sha256": context.canonical_hash,
        "event_sha256": event.canonical_hash,
        "target": bound_target.to_mapping(),
        "observed_at_s": context.captured_at_s,
        "verified_path_length_m": 900.0,
        "minimum_clearance_m": 10.0,
        "source_id": "fixture-segment-clearance",
        "coverage": "entire_compiled_mission",
        "wire_hash": wire_hash,
        "truth_free": True,
        **changes,
    }
    raw = canonical_json(claims).encode()
    return RouteClearanceEvidence(
        claims["context_sha256"],
        claims["event_sha256"],
        GeoPoint.from_mapping(claims["target"]),
        claims["observed_at_s"],
        claims["verified_path_length_m"],
        claims["minimum_clearance_m"],
        claims["source_id"],
        raw,
        hashlib.sha256(raw).hexdigest(),
        claims["wire_hash"],
    )


def _anchor(*, north=20.0, east=0.0, altitude=35.0, context_changes=None):
    bridge, _runner, candidates, runtimes = _bridge()
    bridge.prepare(_context(1.0))
    context = replace(_context(2.0, altitude_m=35.0), **(context_changes or {}))
    executor = bridge.event_loop.adapter.executor
    event = OnboardRuntimeEvent(
        "fixture-source", OnboardEventKind.TARGET_LOST, 2.0, obstacle_distance_m=100.0
    )
    request, _, _, scope = _source(
        2.0,
        context=context,
        event=event,
        scope=executor.model_session_scope(context),
        prompt=executor.prompt,
    )
    worker, clock = _FakeWorker(), _Clock(2.0)
    worker.submit(request, observed_now_s=2.0)
    clock.at_s = 2.05
    worker.finish(
        request,
        clock,
        {
            "response": "replan",
            "reason": "software fixture; not VLM inference",
            "confidence": 0.95,
            "task_action": "search",
            "flight_plan": {
                "action": "search",
                "north_offset_m": north,
                "east_offset_m": east,
                "altitude_m": altitude,
                "speed_mps": None,
                "duration_s": None,
                "reference_id": None,
            },
        },
    )
    snapshot = ExecutionSnapshot(
        context,
        event,
        scope,
        bridge.event_loop.adapter.current_mission.canonical_hash,
        bridge.event_loop.adapter.history[-1].sequence,
        len(bridge.event_loop.adapter.task_state.receipts),
        policy_artifact_sha256=executor.shared_policy.artifact_sha256,
    )
    anchor = SourceAnchoredIntent(
        request,
        worker.result,
        snapshot,
        candidates[0].task,
        canonical_json(runtimes[candidates[0].task.task_id]),
        "fixture-vlm",
        "fixture-v2",
        "a" * 64,
        1,
    )
    return anchor


def _current(anchor, *, north=3.0, east=4.0, at_s=2.1, context_changes=None, event_changes=None):
    position = wgs84_forward_from_ned(
        latitude_deg=anchor.source.context.aircraft_position.latitude_deg,
        longitude_deg=anchor.source.context.aircraft_position.longitude_deg,
        north_offset_m=north,
        east_offset_m=east,
    )
    context = replace(
        anchor.source.context,
        snapshot_id="fixture-new-current",
        captured_at_s=at_s,
        **{
            "aircraft_position": GeoPoint(*position, 35.0, "relative_home"),
            **(context_changes or {}),
        },
    )
    event = replace(
        anchor.source.event, event_id="fixture-current", observed_at_s=at_s, **(event_changes or {})
    )
    current = replace(anchor.source, context=context, event=event, clearance=None)
    try:
        _, compilation = compile_anchored_intent_at_current(anchor, current, mission_id="fixture")
        wire_hash = compilation.wire_hash
    except ValueError:
        # Unqualified fixture state has no admissible route; don't conceal that.
        wire_hash = "f" * 64
    clearance = _clearance(context, event, anchor.target, wire_hash=wire_hash)
    return replace(current, clearance=clearance)


def test_new_origin_does_not_move_old_target_and_binds_current_plan():
    anchor = _anchor()
    assert anchor.source.mission_revision == 0
    original = (
        anchor.request.canonical_hash,
        anchor.result.raw_output,
        anchor.source.context.canonical_hash,
        anchor.source.event.canonical_hash,
        anchor.result.response.to_mapping(),
        anchor.canonical_hash,
    )
    current = _current(anchor)
    admission = admit_anchored_intent(
        anchor, current, now_s=2.2, mission_id="fixture-new", policy=POLICY
    )
    assert admission.effective_plan.north_offset_m == pytest.approx(17.0, abs=0.001)
    assert admission.effective_plan.east_offset_m == pytest.approx(-4.0, abs=0.001)
    waypoints = [x for x in admission.compilation.compiled_mission.wire_items if x.command == 16]
    assert len(waypoints) == 1
    assert (waypoints[0].x, waypoints[0].y) == (
        round(anchor.target.latitude_deg * 1e7),
        round(anchor.target.longitude_deg * 1e7),
    )
    assert admission.compilation.plan.context_snapshot_hash == current.context.canonical_hash
    assert admission.anchor.source.context.canonical_hash != current.context.canonical_hash
    assert original == (
        anchor.request.canonical_hash,
        anchor.result.raw_output,
        anchor.source.context.canonical_hash,
        anchor.source.event.canonical_hash,
        anchor.result.response.to_mapping(),
        anchor.canonical_hash,
    )
    assert admission.to_mapping()["model_saw_execution_context"] is False
    assert admission.to_mapping()["effective_plan_origin"] == "host_geodetic_reprojection"
    assert admission.proposal().reason == anchor.result.response.output.reason


@pytest.mark.parametrize(
    "latitude,longitude",
    [(0, 0), (1.3, 103.8), (55, -120), (-65, 20), (80, 179.9999), (-80, -179.9999)],
)
@pytest.mark.parametrize("north,east", [(20, 0), (0, -25), (100, 200), (-100, -100)])
def test_ellipsoid_inverse_round_trip_at_latitudes_and_date_line(latitude, longitude, north, east):
    origin = GeoPoint(latitude, longitude)
    target = GeoPoint(
        *wgs84_forward_from_ned(
            latitude_deg=latitude, longitude_deg=longitude, north_offset_m=north, east_offset_m=east
        )
    )
    actual = wgs84_inverse_offsets(origin, target)
    assert actual == pytest.approx((north, east), abs=0.0002)


@pytest.mark.parametrize(
    "changes",
    [
        {"gps_fix_type": 2},
        {"gps_horizontal_accuracy_m": 99},
        {"geofence_valid": False},
        {"camera_available": False},
        {"perception_available": False},
        {"map_cache_available": False},
        {"dem_available": False},
    ],
)
def test_current_navigation_fault_rejects_old_clear_source(changes):
    anchor = _anchor()
    with pytest.raises(OnboardVlmAdmissionError, match="navigation/resources"):
        admit_anchored_intent(
            anchor,
            _current(anchor, context_changes=changes),
            now_s=2.2,
            mission_id="fixture",
            policy=POLICY,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"kind": OnboardEventKind.OBSTACLE_RISK},
        {"kind": OnboardEventKind.BATTERY_LOW},
        {"kind": OnboardEventKind.GPS_DEGRADED},
        {"kind": OnboardEventKind.INFERENCE_FAULT},
        {"kind": OnboardEventKind.MISSION_COMPLETE},
        {"obstacle_distance_m": None},
        {"obstacle_distance_m": 5000},
        {"obstacle_distance_m": 1},
    ],
)
def test_current_danger_or_unknown_obstacle_does_not_become_fake_hold(changes):
    anchor = _anchor()
    with pytest.raises(OnboardVlmAdmissionError, match="current event"):
        admit_anchored_intent(
            anchor,
            _current(anchor, event_changes=changes),
            now_s=2.2,
            mission_id="fixture",
            policy=POLICY,
        )


@pytest.mark.parametrize(
    "field,value", [("mission_sha256", "f" * 64), ("mission_revision", 2), ("receipt_revision", 2)]
)
def test_execution_side_effects_invalidate_source(field, value):
    anchor = _anchor()
    with pytest.raises(OnboardVlmAdmissionError, match="Mission/receipt"):
        admit_anchored_intent(
            anchor,
            replace(_current(anchor), **{field: value}),
            now_s=2.2,
            mission_id="fixture",
            policy=POLICY,
        )


def test_clock_epoch_and_task_scope_changes_cannot_rebind_old_pixels():
    anchor = _anchor()
    for field, value in (
        ("clock_epoch", "new-epoch"),
        ("selected_task_id", "different-task"),
        ("session_id", "22222222-2222-4222-8222-222222222222"),
    ):
        current = replace(_current(anchor), scope=replace(anchor.source.scope, **{field: value}))
        with pytest.raises(OnboardVlmAdmissionError, match="scope"):
            admit_anchored_intent(anchor, current, now_s=2.2, mission_id="fixture", policy=POLICY)


def test_changed_home_rejected_at_snapshot_boundary():
    anchor = _anchor()
    with pytest.raises(ValueError, match="profile/home"):
        _current(anchor, context_changes={"home_position": GeoPoint(1.3001, 103.8)})


def test_future_current_stale_current_source_expiry_and_origin_drift_rejected():
    anchor = _anchor()
    for current, now in (
        (_current(anchor), 2.09),
        (_current(anchor), 3.2),
        (_current(anchor, at_s=3.2), 3.3),
        (_current(anchor, north=101), 2.2),
    ):
        with pytest.raises(ValueError):
            admit_anchored_intent(anchor, current, now_s=now, mission_id="fixture", policy=POLICY)


def test_route_clearance_requires_explicit_trusted_source_and_exact_segment():
    anchor, policy = _anchor(), POLICY
    current = _current(anchor)
    with pytest.raises(OnboardVlmAdmissionError, match="unknown"):
        admit_anchored_intent(anchor, current, now_s=2.2, mission_id="fixture")
    with pytest.raises(OnboardVlmAdmissionError, match="unknown"):
        admit_anchored_intent(
            anchor, replace(current, clearance=None), now_s=2.2, mission_id="fixture", policy=policy
        )
    for changes in (
        {"verified_path_length_m": 1},
        {"minimum_clearance_m": 2},
        {"observed_at_s": 0.0},
        {"context_sha256": "f" * 64},
        {"event_sha256": "f" * 64},
        {"target": replace(anchor.target, longitude_deg=103.81).to_mapping()},
    ):
        bad = _clearance(
            current.context,
            current.event,
            anchor.target,
            wire_hash=current.clearance.wire_hash,
            **changes,
        )
        with pytest.raises(OnboardVlmAdmissionError, match="segment"):
            admit_anchored_intent(
                anchor,
                replace(current, clearance=bad),
                now_s=2.2,
                mission_id="fixture",
                policy=policy,
            )


def test_original_clearance_bytes_not_replaceable_with_claims_or_truth():
    anchor = _anchor()
    clearance = _current(anchor).clearance
    with pytest.raises(OnboardVlmAdmissionError, match="hash"):
        replace(clearance, source_receipt_bytes=b"{}")
    original = json.loads(clearance.source_receipt_bytes)
    original["gold_action"] = "search"
    raw = canonical_json(original).encode()
    with pytest.raises(OnboardVlmAdmissionError, match="claims"):
        replace(
            clearance,
            source_receipt_bytes=raw,
            source_receipt_sha256=hashlib.sha256(raw).hexdigest(),
        )


def test_original_worker_identity_and_answer_cannot_change():
    anchor = _anchor()
    with pytest.raises(OnboardVlmAdmissionError, match="generation/request"):
        replace(anchor, expected_generation=2)
    changed_result = replace(
        anchor.result, raw_output=anchor.result.raw_output.replace("0.95", "0.96")
    )
    with pytest.raises(OnboardVlmAdmissionError, match="original raw"):
        replace(anchor, result=changed_result)


def test_altitude_is_anchored_with_original_runtime_policy():
    anchor = _anchor(altitude=300.0)
    assert anchor.target.altitude_m == 120.0
    admission = admit_anchored_intent(
        anchor, _current(anchor), now_s=2.2, mission_id="fixture", policy=POLICY
    )
    assert admission.effective_plan.altitude_m == 120.0


def test_reprojection_outside_output_bound_is_rejected_not_clipped():
    anchor = _anchor(north=500.0)
    with pytest.raises(ValueError):
        admit_anchored_intent(
            anchor,
            _current(anchor, north=-20, east=0),
            now_s=2.2,
            mission_id="fixture",
            policy=POLICY,
        )


def test_inverse_outside_bounded_domain_rejects():
    with pytest.raises(ValueError):
        wgs84_inverse_offsets(GeoPoint(0, 0), GeoPoint(0.1, 0))
    with pytest.raises(ValueError):
        wgs84_inverse_offsets(GeoPoint(90, 0), GeoPoint(89.999, 0))


def test_unfeasible_current_budget_still_uses_canonical_planner():
    anchor = _anchor()
    with pytest.raises(ValueError):
        admit_anchored_intent(
            anchor,
            _current(
                anchor, context_changes={"battery_remaining_pct": 0, "estimated_flight_time_s": 0}
            ),
            now_s=2.2,
            mission_id="fixture",
            policy=POLICY,
        )


def test_climb_and_full_lifecycle_coverage_are_not_a_horizontal_distance_claim():
    anchor = _anchor(altitude=120)
    current = _current(anchor)
    _, compilation = compile_anchored_intent_at_current(anchor, current, mission_id="fixture")
    assert [x.command for x in compilation.compiled_mission.wire_items] == [22, 16, 19, 21]
    assert minimum_compiled_path_length_m(compilation, current.context.aircraft_position) > 200
    insufficient = _clearance(
        current.context,
        current.event,
        anchor.target,
        wire_hash=compilation.wire_hash,
        verified_path_length_m=40,
    )
    with pytest.raises(OnboardVlmAdmissionError, match="segment"):
        admit_anchored_intent(
            anchor,
            replace(current, clearance=insufficient),
            now_s=2.2,
            mission_id="fixture",
            policy=POLICY,
        )


@pytest.mark.parametrize(
    "position",
    [
        GeoPoint(1.3, 103.8),
        GeoPoint(1.3, 103.8, 35, "msl"),
        GeoPoint(1.3, 103.8, 0, "relative_home"),
        GeoPoint(1.3, 103.8, 130, "relative_home"),
    ],
)
def test_current_height_unknown_different_reference_or_out_of_policy_rejects(position):
    anchor = _anchor()
    current = _current(anchor, context_changes={"aircraft_position": position})
    with pytest.raises(OnboardVlmAdmissionError, match="height"):
        admit_anchored_intent(anchor, current, now_s=2.2, mission_id="fixture", policy=POLICY)


def test_full_mission_wire_hash_required_even_when_target_coordinate_agrees():
    anchor = _anchor()
    current = _current(anchor)
    wrong = _clearance(current.context, current.event, anchor.target, wire_hash="f" * 64)
    with pytest.raises(OnboardVlmAdmissionError, match="segment"):
        admit_anchored_intent(
            anchor,
            replace(current, clearance=wrong),
            now_s=2.2,
            mission_id="fixture",
            policy=POLICY,
        )
    # A changed landing/loiter route cannot reuse target-only coverage.
    runtime = anchor.runtime
    runtime["mission_geometry"]["loiter_radius_m"] = 20.0
    changed = replace(anchor, runtime_json=canonical_json(runtime))
    with pytest.raises(OnboardVlmAdmissionError, match="segment"):
        admit_anchored_intent(changed, current, now_s=2.2, mission_id="fixture", policy=POLICY)


def test_admission_reopened_current_and_effective_plan_cannot_be_forged():
    anchor = _anchor()
    admission = admit_anchored_intent(
        anchor, _current(anchor), now_s=2.2, mission_id="fixture", policy=POLICY
    )
    admission.validate_binding(now_s=2.25)
    forged = replace(admission, effective_plan=replace(admission.effective_plan, north_offset_m=5))
    with pytest.raises(OnboardVlmAdmissionError, match="canonical revalidation"):
        forged.validate_binding(now_s=2.25)


def test_bool_worker_generation_cannot_match_integer_slot():
    anchor = _anchor()
    with pytest.raises(OnboardVlmAdmissionError, match="generation/request"):
        replace(anchor, result=replace(anchor.result, generation=True))


@pytest.mark.parametrize(
    "changes",
    [
        {"gps_fix_type": 2},
        {"gps_horizontal_accuracy_m": 99},
        {"aircraft_position": GeoPoint(1.3, 103.8)},
        {"aircraft_position": GeoPoint(1.3, 103.8, 35, "msl")},
    ],
)
def test_unqualified_source_navigation_cannot_become_metric_anchor(changes):
    with pytest.raises(OnboardVlmAdmissionError, match="source navigation"):
        _anchor(context_changes=changes)


def test_changed_shared_policy_artifact_invalidates_unmodified_mission():
    anchor = _anchor()
    current = replace(_current(anchor), policy_artifact_sha256="f" * 64)
    with pytest.raises(OnboardVlmAdmissionError, match="Mission/receipt"):
        admit_anchored_intent(anchor, current, now_s=2.2, mission_id="fixture", policy=POLICY)
    with pytest.raises(ValueError):
        replace(anchor.source, policy_artifact_sha256=None)


def test_current_camera_relative_metric_target_cannot_trigger_old_executor_veto_after_admission():
    anchor = _anchor()
    current = _current(
        anchor,
        event_changes={"target_geometry_frame": "camera_relative", "target_distance_m": 15.0},
    )
    with pytest.raises(OnboardVlmAdmissionError, match="current event"):
        admit_anchored_intent(anchor, current, now_s=2.2, mission_id="fixture", policy=POLICY)


def test_source_confidence_below_existing_gate_is_rejected_before_adapter():
    anchor = _anchor()
    raw = anchor.result.raw_output.replace("0.95", "0.1")
    from multidetect.autonomy.onboard_vlm_runtime_contract import parse_onboard_vlm_runtime_response

    response = parse_onboard_vlm_runtime_response(
        raw,
        anchor.request,
        current_scope=anchor.source.scope,
        completed_at_s=anchor.result.completed_at_s,
        inference_latency_ms=anchor.result.inference_latency_ms,
    )
    with pytest.raises(OnboardVlmAdmissionError, match="confidence gate"):
        replace(anchor, result=replace(anchor.result, raw_output=raw, response=response))
