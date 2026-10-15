from __future__ import annotations

import math

from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.onboard_loop import CameraModel, OnboardLoop
from multidetect.autonomy.safety_envelope import SafetyEnvelope, SafetyEnvelopeConfig
from multidetect.sim_teacher.primitives import ActionPrimitive

CAMERA = CameraModel(fx=540, fy=540, cx=320, cy=240, width=640, height=480)


def _pose(north=0.0, east=0.0, altitude=10.0):
    return (float(north), float(east), -float(altitude))


def test_default_envelope_preserves_existing_bounded_primitives():
    envelope = SafetyEnvelope()
    for action in (
        ActionPrimitive.translate(15.0, 0.0),
        ActionPrimitive.turn(90.0),
        ActionPrimitive.hold(1.0),
        ActionPrimitive.stop(),
    ):
        decision = envelope.apply(action, pose_ned=_pose(), yaw_deg=0.0)
        assert decision.action.to_dict() == action.to_dict()
        assert decision.status == "accepted"

    clipped = envelope.apply(ActionPrimitive.translate(20.0, 0.0), pose_ned=_pose(), yaw_deg=0.0)
    assert clipped.status == "clipped"
    assert math.isclose(clipped.action.forward_m, 15.0)
    assert clipped.reasons == ("max_translation",)


def test_altitude_limits_clip_vertical_translation_and_reject_wrong_direction():
    envelope = SafetyEnvelope(SafetyEnvelopeConfig(min_altitude_m=8.0, max_altitude_m=12.0))
    low = envelope.apply(
        ActionPrimitive.translate(4.0, 0.0, 6.0), pose_ned=_pose(altitude=5), yaw_deg=0
    )
    assert low.status == "clipped"
    assert low.action.forward_m == 0.0
    assert low.action.up_m == 3.0
    assert "min_altitude" in low.reasons

    high = envelope.apply(
        ActionPrimitive.translate(4.0, 0.0, -6.0), pose_ned=_pose(altitude=15), yaw_deg=0
    )
    assert high.status == "clipped"
    assert high.action.forward_m == 0.0
    assert high.action.up_m == -3.0
    rejected = envelope.apply(
        ActionPrimitive.translate(1.0, 0.0), pose_ned=_pose(altitude=5), yaw_deg=0
    )
    assert rejected.rejected
    assert rejected.reasons == ("min_altitude",)


def test_geofence_and_home_radius_clip_to_the_last_safe_prefix():
    fence = SafetyEnvelope(
        SafetyEnvelopeConfig(operating_polygon=((0, 0), (10, 0), (10, 10), (0, 10)))
    )
    decision = fence.apply(ActionPrimitive.translate(15, 0), pose_ned=_pose(5, 5), yaw_deg=0)
    assert decision.status == "clipped"
    assert decision.action.forward_m < 5.1
    assert "geofence" in decision.reasons

    radius = SafetyEnvelope(SafetyEnvelopeConfig(max_radius_from_home_m=5.0))
    radius.set_home((0.0, 0.0))
    decision = radius.apply(ActionPrimitive.translate(10, 0), pose_ned=_pose(), yaw_deg=0)
    assert decision.status == "clipped"
    assert decision.action.forward_m < 5.1
    assert "home_radius" in decision.reasons


def test_concave_geofence_checks_the_segment_not_only_its_endpoint():
    # The route from (1, 1) to (9, 9) crosses the missing lower-right notch.
    polygon = ((0, 0), (10, 0), (10, 4), (4, 4), (4, 10), (0, 10))
    envelope = SafetyEnvelope(SafetyEnvelopeConfig(operating_polygon=polygon))
    decision = envelope.apply(ActionPrimitive.translate(8, 8), pose_ned=_pose(1, 1), yaw_deg=0)
    assert decision.status == "clipped"
    assert "geofence" in decision.reasons
    assert decision.action.forward_m < 8.0


def test_static_obstacle_clips_or_falls_back_to_hover():
    envelope = SafetyEnvelope(
        SafetyEnvelopeConfig(max_translation_m=20),
        obstacle_check=lambda pose, north, east: north <= 4.0,
    )
    clipped = envelope.apply(ActionPrimitive.translate(10, 0), pose_ned=_pose(), yaw_deg=0)
    assert clipped.status == "clipped"
    assert clipped.action.forward_m < 4.1
    assert "static_obstacle" in clipped.reasons

    blocked = SafetyEnvelope(
        SafetyEnvelopeConfig(max_translation_m=20), obstacle_check=lambda *_: False
    )
    rejected = blocked.apply(ActionPrimitive.translate(2, 0), pose_ned=_pose(), yaw_deg=0)
    assert rejected.rejected
    assert rejected.action.kind == "hold"
    assert "static_obstacle" in rejected.reasons


def test_safety_labels_and_rejection_counter_include_optional_rth_signal():
    envelope = SafetyEnvelope(
        SafetyEnvelopeConfig(
            operating_polygon=((0, 0), (1, 0), (1, 1), (0, 1)), rth_after_rejections=2
        )
    )
    first = envelope.apply(ActionPrimitive.translate(-2, 0), pose_ned=_pose(), yaw_deg=0)
    second = envelope.apply(ActionPrimitive.translate(-2, 0), pose_ned=_pose(), yaw_deg=0)
    assert first.info()["safety"]["reasons"] == ["geofence"]
    assert first.consecutive_rejections == 1
    assert second.return_home is True
    assert second.info()["safety"]["return_home"] is True
    accepted = envelope.apply(ActionPrimitive.hold(1), pose_ned=_pose(), yaw_deg=0)
    assert accepted.consecutive_rejections == 0


def test_body_frame_translation_uses_yaw_for_geofence_checks():
    envelope = SafetyEnvelope(
        SafetyEnvelopeConfig(operating_polygon=((-1, -10), (1, -10), (1, 10), (-1, 10)))
    )
    decision = envelope.apply(ActionPrimitive.translate(10, 0), pose_ned=_pose(), yaw_deg=90)
    assert decision.status == "accepted"
    assert math.isclose(decision.action.forward_m, 10.0)


def test_default_envelope_replays_executor_script_without_changing_primitives():
    plan = {
        "plan_version": 1,
        "goal": "goto target",
        "steps": [
            {
                "id": "go",
                "skill": "goto",
                "params": {"target_ref": "poi:target"},
                "on_success": "home",
                "on_fail": "home",
            },
            {
                "id": "home",
                "skill": "return_home",
                "params": {},
                "on_success": None,
                "on_fail": None,
            },
        ],
        "report": ["status"],
    }

    def factory(_task_text, **_overrides):
        return OnboardLoop(
            camera=CAMERA,
            search_action=lambda _pose, _yaw: ActionPrimitive.translate(1, 0),
            segment_clear=lambda _pose, _north, _east: True,
        )

    def run(with_envelope):
        executor = MissionExecutor(plan, loop_factory=factory, refs={"poi:target": (10.0, 0.0)})
        pose = [0.0, 0.0, -10.0]
        actions = []
        envelope = SafetyEnvelope() if with_envelope else None
        for _ in range(20):
            action, _ = executor.next_action(pose_ned=tuple(pose), yaw_deg=0.0)
            if envelope is not None:
                action = envelope.apply(action, pose_ned=tuple(pose), yaw_deg=0.0).action
            actions.append(action.to_dict())
            if action.kind == "translate":
                pose[0] += action.forward_m
                pose[1] += action.right_m
                pose[2] -= action.up_m
            if action.kind == "stop":
                break
        return actions

    assert run(False) == run(True)
