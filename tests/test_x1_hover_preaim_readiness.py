import math
from types import SimpleNamespace

import pytest

from multidetect.sim_teacher.gimbal_recording import (
    X1HoverPreAimReadiness,
    X1YawUnwind,
    x1_eval_yaw_unwind,
    x1_fw_track_preaim,
    x1_meta_yaw_for_step,
    x1_track_preaim,
)
from scripts.collect_sim_teacher_episodes import _x1_hover_preaim_measurements


class FakeClock:
    def __init__(self, now=10.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def meta(stamp_ns, *, yaw_deg=0.0, fresh=True):
    return SimpleNamespace(
        sensor_stamp_ns=stamp_ns,
        joint_state={
            "gimbal_joint_state": {
                "fresh": fresh,
                "position_rad": {"yaw": yaw_deg * 3.141592653589793 / 180.0},
            }
        },
    )


def passing_sample(readiness, clock, stamp_ns, *, in_view=True, error_deg=2.9):
    clock.advance(0.1)
    return readiness.observe(
        meta(stamp_ns), in_view=in_view, pointing_error_deg=error_deg
    )


def test_hover_readiness_requires_five_fresh_post_step_10hz_samples():
    clock = FakeClock()
    start = 2_000_000_000
    readiness = X1HoverPreAimReadiness(start, start, clock=clock)

    # The FW frame still in latest_meta at step entry is not a new pre-aim epoch.
    stale = readiness.observe(meta(start), in_view=True, pointing_error_deg=0.5)
    assert stale["consecutive_samples"] == 0
    assert not stale["ready"]

    for index in range(1, 5):
        result = passing_sample(readiness, clock, start + index * 100_000_000)
        assert result["consecutive_samples"] == index
        assert not result["ready"]

    result = passing_sample(readiness, clock, start + 500_000_000)
    assert result["ready"]
    assert result["settle_wall_s"] == pytest.approx(0.5)
    assert result["last_yaw_deg"] == pytest.approx(0.0)
    assert result["last_in_view"] is True
    assert result["last_pointing_error_deg"] == pytest.approx(2.9)

    readiness.restart_confirmation()
    assert not readiness.ready
    for index in range(6, 10):
        result = passing_sample(readiness, clock, start + index * 100_000_000)
        assert result["consecutive_samples"] == index - 5
        assert not result["ready"]
    result = passing_sample(readiness, clock, start + 1_000_000_000)
    assert result["ready"]
    assert result["readiness_wait_elapsed_s"] == pytest.approx(0.5)

    # Readiness is a trailing condition at the step-9 boundary, not a latch from early pre-aim.
    result = readiness.observe(
        meta(start + 1_100_000_000), in_view=False, pointing_error_deg=0.1
    )
    assert not result["ready"]
    assert result["consecutive_samples"] == 0


def test_hover_readiness_resets_on_each_failed_condition_and_missed_10hz_sample():
    clock = FakeClock()
    start = 5_000_000_000
    readiness = X1HoverPreAimReadiness(start, start, clock=clock)

    sample = readiness.observe(
        meta(start + 100_000_000, yaw_deg=150), in_view=True, pointing_error_deg=0.1
    )
    assert sample["consecutive_samples"] == 0
    assert "joint_yaw_outside_margin" in sample["last_failure_reasons"]

    sample = readiness.observe(
        meta(start + 200_000_000), in_view=True, pointing_error_deg=3.0
    )
    assert sample["consecutive_samples"] == 0
    assert "pointing_error_not_below_threshold" in sample["last_failure_reasons"]

    sample = readiness.observe(
        meta(start + 300_000_000), in_view=False, pointing_error_deg=0.1
    )
    assert sample["consecutive_samples"] == 0
    assert "target_not_in_view" in sample["last_failure_reasons"]

    sample = readiness.observe(
        meta(start + 400_000_000, fresh=False), in_view=True, pointing_error_deg=0.1
    )
    assert sample["consecutive_samples"] == 0
    assert "joint_state_not_fresh" in sample["last_failure_reasons"]

    passing_sample(readiness, clock, start + 500_000_000)
    passing_sample(readiness, clock, start + 600_000_000)
    # A missing 10 Hz epoch breaks consecutiveness; the current good frame starts a new run.
    sample = passing_sample(readiness, clock, start + 800_000_000)
    assert sample["consecutive_samples"] == 1
    assert not sample["ready"]
    sample = passing_sample(readiness, clock, start + 810_000_000)
    assert sample["consecutive_samples"] == 1
    assert not sample["ready"]


def test_hover_readiness_wait_timeout_is_bounded_and_starts_after_preaim():
    clock = FakeClock()
    readiness = X1HoverPreAimReadiness(1_000_000_000, clock=clock)
    clock.advance(25.0)  # The unscored pre-aim itself may take much longer than the settle wait.
    assert not readiness.timed_out()

    readiness.begin_wait()
    clock.advance(readiness.WAIT_TIMEOUT_S - 0.01)
    assert not readiness.timed_out()
    clock.advance(0.01)
    assert readiness.timed_out()
    assert readiness.diagnostics()["readiness_wait_elapsed_s"] == pytest.approx(10.0)


def test_only_fw_handoff_preaim_uses_track_and_non_fw_unwind_is_unchanged():
    action = SimpleNamespace(reason="hover_return_to_home")
    assert x1_track_preaim(8, 7, action)
    assert not x1_fw_track_preaim(0, 8, 7, action)
    assert x1_fw_track_preaim(1, 8, 7, action)
    assert not x1_fw_track_preaim(1, 8, 6, action)

    unwind = x1_eval_yaw_unwind(8, 7, action, fw_leg=False)
    assert isinstance(unwind, X1YawUnwind)
    assert x1_eval_yaw_unwind(8, 7, action, fw_leg=True) is None


def test_step_yaw_rejects_stale_fw_getmeta_at_hover_entry():
    start = 8_000_000_000
    assert x1_meta_yaw_for_step(meta(start - 100_000_000), start) is None
    assert x1_meta_yaw_for_step(meta(start, yaw_deg=-12), start) == pytest.approx(
        -12 * 3.141592653589793 / 180
    )
    assert x1_meta_yaw_for_step(meta(start + 100_000_000, fresh=False), start) is None


@pytest.mark.parametrize(("stale_yaw", "first_fresh_after_s"), [(-247.7, 0.16), (-216.7, 0.22)])
def test_yaw_unwind_replay_ignores_fw_cache_even_when_stamp_overlaps_step(stale_yaw, first_fresh_after_s):
    start = 8_000_000_000
    cached = meta(start, yaw_deg=stale_yaw)
    unwind = X1YawUnwind()
    yaw = x1_meta_yaw_for_step(cached, start, previous_meta_stamp_ns=cached.sensor_stamp_ns)
    assert unwind.step(yaw, 0.) is None
    assert not unwind.active
    fresh = meta(start + round(first_fresh_after_s * 1e9), yaw_deg=-1.3)
    yaw = x1_meta_yaw_for_step(fresh, start, previous_meta_stamp_ns=cached.sensor_stamp_ns)
    assert unwind.step(yaw, first_fresh_after_s) is None
    assert not unwind.active

    genuinely_wound = meta(fresh.sensor_stamp_ns + 100_000_000, yaw_deg=-190.)
    yaw = x1_meta_yaw_for_step(genuinely_wound, start, previous_meta_stamp_ns=fresh.sensor_stamp_ns)
    assert unwind.step(yaw, 1.) == "lock"
    assert unwind.step(math.radians(-5.), 1.25) == "track"


def test_yaw_unwind_expires_when_readback_stops():
    unwind = X1YawUnwind()
    assert unwind.step(math.radians(190.), 1.0) == "lock"
    assert unwind.step(None, 1.0 + X1YawUnwind.MAX_S + 0.01) == "track"
    assert not unwind.active


def test_hover_preaim_geometry_uses_frame_aligned_target_and_camera():
    row = {
        "camera_pose": {
            "age_ms": 0.0,
            "center_ned": [0.0, 0.0, 0.0],
            "optical_to_world": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        },
        "pose": {"global_position_skew_ms": 0.0},
        "camera_info": {
            "fx": 100.0, "fy": 100.0, "cx": 50.0, "cy": 50.0,
            "width": 100, "height": 100,
        },
    }
    visible, error = _x1_hover_preaim_measurements(
        row, {"kind": "direction_ned", "vector": [1.0, 0.0, 0.0]}
    )
    assert visible is True
    assert error == pytest.approx(0.0)

    visible, error = _x1_hover_preaim_measurements(
        row, {"kind": "direction_ned", "vector": [0.0, 1.0, 0.0]}
    )
    assert visible is False
    assert error == pytest.approx(90.0)
