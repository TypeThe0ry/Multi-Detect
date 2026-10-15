from __future__ import annotations

from multidetect.autonomy.safety_envelope import SafetyEnvelope, SafetyEnvelopeConfig
from multidetect.sim_teacher.primitives import ActionPrimitive
from scripts.onboard_policy import OnboardPolicy


class _Blocked:
    def segment_clear(self, start_ned, end_ned):
        return False


def test_mixed_forward_down_uses_horizontal_limit_only():
    envelope = SafetyEnvelope(SafetyEnvelopeConfig(max_horizontal_step_m=15, max_vertical_step_m=10), home_ned=(0, 0, 0))
    decision = envelope.apply(ActionPrimitive.translate(12, 0, -8), pose_ned=(0, 0, -20), yaw_deg=0)
    assert decision.action == ActionPrimitive.translate(12, 0, -8)
    assert decision.status == "accepted"


def test_long_climb_is_split_and_segments_complete():
    envelope = SafetyEnvelope(SafetyEnvelopeConfig(max_vertical_step_m=10), home_ned=(0, 0, 0))
    decision = envelope.apply(ActionPrimitive.translate(0, 0, 25), pose_ned=(0, 0, -5))
    assert round(sum(s.up_m for s in decision.segments), 6) == 25
    assert len(decision.segments) == 3
    assert max(abs(s.up_m) for s in decision.segments) <= 10


def test_home_relative_altitude_and_agl_are_distinct():
    envelope = SafetyEnvelope(
        SafetyEnvelopeConfig(min_altitude_m=5, min_agl_m=3), home_ned=(100, 200, -2)
    )
    # Home-relative altitude is 2 m, so this is raised to 5 m.
    decision = envelope.apply(ActionPrimitive.hold(1), pose_ned=(100, 200, 0), current_agl_m=4)
    assert decision.rejected
    # At home-relative 5 m, AGL still vetoes when telemetry says 2 m.
    decision = envelope.apply(ActionPrimitive.hold(1), pose_ned=(100, 200, -7), current_agl_m=2)
    assert decision.rejected


def test_deliver_descent_allows_two_meter_release():
    envelope = SafetyEnvelope(SafetyEnvelopeConfig(min_altitude_m=8), home_ned=(0, 0, 0))
    decision = envelope.apply(
        ActionPrimitive.translate(0, 0, -6),
        pose_ned=(0, 0, -8),
        skill="deliver",
        phase="descent",
    )
    assert not decision.rejected
    assert decision.action.up_m == -6


def test_no_fly_and_obstacle_source_reject_without_mutating_executor():
    config = SafetyEnvelopeConfig(
        operating_polygon=((-10, -10), (10, -10), (10, 10), (-10, 10)),
        no_fly_polygons=(((1, -1), (3, -1), (3, 1), (1, 1)),),
    )
    envelope = SafetyEnvelope(config, home_ned=(0, 0, 0), obstacle_source=_Blocked())
    decision = envelope.apply(ActionPrimitive.translate(5, 0), pose_ned=(0, 0, -8))
    assert decision.rejected
    assert {"no_fly", "obstacle"} <= set(decision.reasons)


def test_home_is_explicit_and_never_taken_from_first_pose():
    envelope = SafetyEnvelope(SafetyEnvelopeConfig(max_radius_from_home_m=10.0))
    decision = envelope.apply(ActionPrimitive.translate(1, 0), pose_ned=(100, 100, -5))
    assert not decision.rejected
    assert envelope.home_ned is None

    anchored = SafetyEnvelope(
        SafetyEnvelopeConfig(max_radius_from_home_m=10.0), home_ned=(0, 0, 0)
    )
    assert anchored.apply(ActionPrimitive.translate(1, 0), pose_ned=(100, 100, -5)).rejected
    assert anchored.effective_radius_from_home() == 10.0


def test_vertical_segments_reach_target_and_report_metadata():
    envelope = SafetyEnvelope(SafetyEnvelopeConfig(max_vertical_step_m=10), home_ned=(0, 0, 0))
    pose = (0.0, 0.0, -5.0)
    decision = envelope.apply(ActionPrimitive.translate(0, 0, 25), pose_ned=pose)
    for segment in decision.segments:
        pose = (pose[0], pose[1], pose[2] - segment.up_m)
        assert abs(segment.up_m) <= 10
    assert pose[2] == -30.0
    assert len(decision.info()["safety"]["segments"]) == 3


def test_default_enabled_policy_is_byte_identical_for_valid_actions():
    actions = (
        ActionPrimitive.translate(5, 2, -3),
        ActionPrimitive.turn(30),
        ActionPrimitive.hold(0.5),
        ActionPrimitive.stop(),
    )

    def make(enabled):
        policy = OnboardPolicy.__new__(OnboardPolicy)
        policy.safety_envelope = (
            SafetyEnvelope(SafetyEnvelopeConfig(), home_ned=(0, 0, 0)) if enabled else None
        )
        policy._act_impl = lambda observation, *, pose_ned, yaw_deg: (actions[observation["i"]], {})
        return policy

    enabled, disabled = make(True), make(False)
    for i in range(len(actions)):
        kwargs = {"observation": {"i": i}, "pose_ned": (0, 0, -10), "yaw_deg": 0}
        enabled_action, _enabled_labels = enabled.act(**kwargs)
        disabled_action, _disabled_labels = disabled.act(**kwargs)
        assert enabled_action.to_dict() == disabled_action.to_dict()


def test_act_single_exit_arbiter_covers_supervisor_health_and_detector_holds():
    class CountingEnvelope:
        def __init__(self):
            self.calls = 0

        def apply(self, action, **kwargs):
            self.calls += 1
            from multidetect.autonomy.safety_envelope import SafetyDecision

            return SafetyDecision(action, "accepted")

    policy = OnboardPolicy.__new__(OnboardPolicy)
    envelope = CountingEnvelope()
    policy.safety_envelope = envelope
    actions = [ActionPrimitive.hold(1), ActionPrimitive.hold(1), ActionPrimitive.translate(1, 0)]
    policy._act_impl = lambda observation, *, pose_ned, yaw_deg: (actions[observation["i"]], {"onboard": observation["labels"]})
    for i, label in enumerate(("supervised", "health_hold", "detector_skip")):
        action, _ = policy.act({"i": i, "labels": {"state": label}}, pose_ned=(0, 0, -5), yaw_deg=0)
        assert action == actions[i]
    assert envelope.calls == 3
