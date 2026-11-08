"""CPU-only closed-loop geometry, fallback and legacy-output regression checks."""

import math
from dataclasses import asdict

import pytest

import multidetect.autonomy.onboard_loop as onboard_loop_module
from multidetect.autonomy.onboard_loop import CameraModel, Detection, LoopConfig, OnboardLoop
from multidetect.autonomy.step_triangulation import PositionEstimate
from multidetect.sim_teacher.primitives import ActionPrimitive

CAMERA = CameraModel(500, 500, 500, 400, 1000, 800)
TARGET = (30.0, 0.0, -1.0)
GROUND = (34.0, 0.0)
# About 15 degrees of parallax at 30 m: clears the always-on distance gate.
WIDE = ((0.0, -9.0, -10.0), (0.0, 9.0, -10.0))


def loop(**options):
    return OnboardLoop(
        CAMERA,
        lambda p, y: ActionPrimitive.hold(1),
        segment_clear=lambda *args: True,
        config=LoopConfig(**options),
    )


def detection(pose):
    f, r, d = (TARGET[i] - pose[i] for i in range(3))
    c, s = math.cos(CAMERA.pitch_down_rad), math.sin(CAMERA.pitch_down_rad)
    depth = c * f + s * d
    u, v = CAMERA.cx + CAMERA.fx * r / depth, CAMERA.cy + CAMERA.fy * (-s * f + c * d) / depth
    return Detection((u - 20, v - 20, u + 20, v + 20), 0.9)


def observe(subject, pose, *, ray=True, stamp=None):
    kwargs = dict(pose_ned=pose, yaw_deg=0, ground_point=lambda u, v: GROUND)
    if ray:
        kwargs.update(
            camera_ray=lambda u, v: (pose, CAMERA.ray_ned(u, v, 0)),
            timestamp_s=subject.step_index if stamp is None else stamp,
        )
    return subject.update_tracks([detection(pose)], **kwargs)


def act(subject, tracks, pose, ground=GROUND):
    subject.apply_verdict(tracks[0].track_id, "confirm_target")
    return subject.next_action(tracks, pose_ned=pose, yaw_deg=0, ground_point=lambda u, v: ground)


@pytest.mark.parametrize("count", [False, True])
def test_disabled_is_identical_including_records_association_and_memory(count):
    original = loop(stop_on_confirm=not count, memory_enabled=True)
    disabled = loop(step_triangulation=False, stop_on_confirm=not count, memory_enabled=True)
    for pose in ((0.0, -9.0, -10.0), (0.0, 9.0, -10.0)):
        a = observe(original, pose, ray=False)

        def forbidden(*args):
            raise AssertionError("disabled geometry callback must never run")

        b = disabled.update_tracks(
            [detection(pose)],
            pose_ned=pose,
            yaw_deg=0,
            ground_point=lambda u, v: GROUND,
            camera_ray=forbidden,
        )
        assert [asdict(t) for t in a] == [asdict(t) for t in b]
        assert act(original, a, pose) == act(disabled, b, pose)
        assert asdict(original._place_memory) == asdict(disabled._place_memory)
    record = disabled.confirmations[0] if count else disabled.confirmed
    assert record["position_estimate_ne"] == list(GROUND)
    assert "position_source" not in record
    assert disabled.tracks["t1"].position_views == []


def test_large_ground_anchor_is_shifted_to_object_centre_without_moving_association():
    camera = CameraModel(500, 500, 1000, 400, 2000, 800)
    subject = OnboardLoop(
        camera,
        lambda p, y: ActionPrimitive.hold(1),
        segment_clear=lambda *args: True,
        config=LoopConfig(
            step_triangulation=True,
            ground_extent_size_m=12.0,
            ground_height_sigma_m=5.0,
        ),
    )
    box = Detection((500.0, 300.0, 1500.0, 500.0), 0.9)
    pose = (0.0, 0.0, -10.0)
    tracks = subject.update_tracks(
        [box],
        pose_ned=pose,
        yaw_deg=0.0,
        ground_point=lambda u, v: (10.0, 0.0),
        camera_ray=lambda u, v: (pose, (0.8944271909999159, 0.0, 0.4472135954999579)),
    )
    track = tracks[0]
    assert track.ground == (10.0, 0.0)
    assert track.position_estimate is not None
    assert track.position_estimate.source == "ground"
    assert track.position_estimate.position_ned[:2] == pytest.approx((16.0, 0.0), abs=0.05)


def test_cone_extent_correction_is_skipped():
    subject = loop(step_triangulation=True, ground_extent_size_m=0.5)
    track = subject.update_tracks(
        [detection((0.0, 0.0, -10.0))],
        pose_ned=(0.0, 0.0, -10.0),
        yaw_deg=0.0,
        ground_point=lambda u, v: GROUND,
        camera_ray=lambda u, v: ((0.0, 0.0, -10.0), (0.8944271909999159, 0.0, 0.4472135954999579)),
    )[0]
    assert track.position_estimate is not None
    assert track.position_estimate.position_ned[:2] == pytest.approx(GROUND)


def test_fused_estimate_is_not_shifted_a_second_time(monkeypatch):
    subject = loop(step_triangulation=True, ground_extent_size_m=12.0, ground_height_sigma_m=20.0)
    fused = PositionEstimate(
        (16.0, 0.0, -1.0),
        0.2,
        1.0,
        "triangulation",
        view_count=2,
        baseline_m=8.0,
        ray_angle_deg=20.0,
    )
    monkeypatch.setattr(onboard_loop_module, "triangulate_views", lambda *args, **kwargs: fused)
    for step, pose in enumerate(((0.0, -4.0, -10.0), (0.0, 4.0, -10.0))):
        subject.step_index = step
        tracks = subject.update_tracks(
            [detection(pose)],
            pose_ned=pose,
            yaw_deg=0.0,
            ground_point=lambda u, v: GROUND,
            camera_ray=lambda u, v, pose=pose: (pose, CAMERA.ray_ned(u, v, 0)),
        )
    estimate = tracks[0].position_estimate
    assert estimate is not None and estimate.source == "fused"
    # The triangulated centre is already corrected; applying the extent shift
    # again would put the result near 22 m instead of the 16 m centre.
    assert subject.target_position(tracks[0]) == pytest.approx((16.0, 0.0), abs=0.01)


@pytest.mark.parametrize("count", [False, True])
def test_multistep_estimate_is_reported_without_changing_tracking_ground(count):
    subject = loop(step_triangulation=True, ground_height_sigma_m=20, stop_on_confirm=not count)
    for pose in WIDE:
        current = observe(subject, pose)
        assert current[0].track_id == "t1"
        assert current[0].ground == GROUND
        action, info = act(subject, current, pose)
    record = subject.confirmations[0] if count else subject.confirmed
    assert record["position_estimate_ne"] == pytest.approx(TARGET[:2], abs=0.05)
    assert record["position_source"] == "fused"
    assert record["position_sigma_m"] > 0
    assert record["position_view_count"] == 2
    assert record["position_baseline_m"] == pytest.approx(18)
    assert record["position_ray_angle_deg"] > 10
    assert record["position_consistent"] is True
    assert info["horizontal_m"] == pytest.approx(
        math.dist(record["position_estimate_ne"], pose[:2])
    )


@pytest.mark.parametrize("pure_turn", [False, True])
def test_single_step_or_pure_turn_falls_back_to_exact_ground(pure_turn):
    subject = loop(step_triangulation=True, confirm_matches=1)
    pose = (0.0, 0.0, -10.0)
    current = observe(subject, pose)
    if pure_turn:
        subject.step_index += 1
        # Different heading/pixels cannot manufacture a translation baseline.
        current = subject.update_tracks(
            [detection(pose)],
            pose_ned=pose,
            yaw_deg=15,
            ground_point=lambda u, v: GROUND,
            camera_ray=lambda u, v: (pose, CAMERA.ray_ned(u, v, 15)),
            timestamp_s=1,
        )
    act(subject, current, pose)
    assert subject.confirmed["position_estimate_ne"] == list(GROUND)
    assert subject.confirmed["position_source"] == "ground"
    assert current[0].position_estimate.source == "ground"


def test_distance_aware_parallax_gate_rejects_far_near_collinear_update(monkeypatch):
    subject = loop(step_triangulation=True)
    fake = PositionEstimate(
        (GROUND[0] + 1.0, GROUND[1], -1.0),
        0.2,
        1.0,
        "triangulation",
        view_count=4,
        baseline_m=30.0,
        ray_angle_deg=5.0,
    )
    monkeypatch.setattr(onboard_loop_module, "triangulate_views", lambda *args, **kwargs: fake)
    for pose in ((0.0, -1.0, -10.0), (0.0, 1.0, -10.0)):
        current = observe(subject, pose)
    assert current[0].position_estimate is not None
    assert current[0].position_estimate.source == "ground"
    assert current[0].position_estimate.consistency_note == (
        "parallax_below_distance_gate:fallback_ground"
    )


def test_large_ground_sigma_no_longer_bypasses_the_parallax_gate():
    # Formerly ground_height_sigma_m > 10 disabled both gates; about 3.8
    # degrees of parallax at 30 m now falls back to the ground hit.
    subject = loop(step_triangulation=True, ground_height_sigma_m=20)
    for step, pose in enumerate(((0.0, -1.0, -10.0), (0.0, 1.0, -10.0))):
        subject.step_index = step
        current = observe(subject, pose)
    assert len(current[0].position_views) == 2
    assert current[0].position_estimate.source == "ground"
    assert current[0].position_estimate.consistency_note == (
        "parallax_below_distance_gate:fallback_ground"
    )
    assert subject.target_position(current[0]) == GROUND


@pytest.mark.parametrize(
    "ground_sigma,gate_sigma,corrected",
    [
        # Default k=2.5: 1 m of ground-height sigma on a ~19 degree bottom ray
        # allows about 7 m of horizontal correction, so the 4 m one is used.
        (1.0, 2.5, True),
        # A trusted ground (small sigma) keeps the anchor pinned.
        (0.25, 2.5, False),
        # k=0 is exactly the former fixed 0.1 m gate.
        (1.0, 0.0, False),
    ],
)
def test_anchor_gate_scales_with_ground_horizontal_sigma(
    monkeypatch, ground_sigma, gate_sigma, corrected
):
    subject = loop(
        step_triangulation=True,
        ground_height_sigma_m=ground_sigma,
        step_triangulation_ground_gate_sigma=gate_sigma,
    )
    fake = PositionEstimate(
        TARGET, 0.2, 1.0, "triangulation", view_count=2, baseline_m=8.0, ray_angle_deg=15.0
    )
    monkeypatch.setattr(onboard_loop_module, "triangulate_views", lambda *args, **kwargs: fake)
    for pose in WIDE:
        current = observe(subject, pose)
    estimate = current[0].position_estimate
    assert current[0].ground == GROUND
    if corrected:
        assert estimate.source == "fused" and estimate.consistent
        assert subject.target_position(current[0]) == pytest.approx(TARGET[:2], abs=0.1)
    else:
        assert estimate.source == "ground" and not estimate.consistent
        assert estimate.consistency_note == "ground_anchor_disagreement:fallback_ground"
        assert subject.target_position(current[0]) == GROUND


def test_repeated_step_does_not_add_views_and_failed_new_ray_drops_old_estimate():
    subject = loop(step_triangulation=True, ground_height_sigma_m=20)
    observe(subject, WIDE[0])
    track = observe(subject, WIDE[0])[0]
    assert len(track.position_views) == 1
    subject.step_index += 1
    observe(subject, WIDE[1])
    assert track.position_estimate.source == "fused"
    subject.step_index += 1
    subject.update_tracks(
        [detection((0.0, 5.0, -10.0))],
        pose_ned=(0.0, 5.0, -10.0),
        yaw_deg=0,
        ground_point=lambda u, v: GROUND,
        camera_ray=lambda u, v: None,
        timestamp_s=2,
    )
    assert track.position_estimate is None
    assert subject.target_position(track) == GROUND


def test_history_is_bounded_by_steps_and_backward_clock_resets_views():
    subject = loop(step_triangulation=True, step_triangulation_max_views=2)
    for step in range(4):
        subject.step_index = step
        track = observe(subject, (0.0, float(step), -10.0))[0]
    assert [v.step for v in track.position_views] == [2, 3]
    subject.step_index = 4
    track = observe(subject, (0.0, 4.0, -10.0), stamp=1)[0]
    assert [v.step for v in track.position_views] == [4]


@pytest.mark.parametrize("error", [KeyError, AttributeError, RuntimeError, ValueError])
def test_camera_callback_failure_falls_back_without_interrupting_loop(error):
    subject = loop(step_triangulation=True, confirm_matches=1)
    pose = (0.0, 0.0, -10.0)

    def broken_ray(*args):
        raise error("missing camera calibration")

    current = subject.update_tracks(
        [detection(pose)],
        pose_ned=pose,
        yaw_deg=0,
        ground_point=lambda u, v: GROUND,
        camera_ray=broken_ray,
    )
    act(subject, current, pose)
    assert subject.confirmed["position_estimate_ne"] == list(GROUND)
    assert subject.confirmed["position_source"] == "ground"
    assert current[0].position_estimate is None


@pytest.mark.parametrize("multistep", [False, True])
def test_vertical_bottom_ray_does_not_invent_ground_height(multistep):
    subject = loop(step_triangulation=True)
    poses = WIDE if multistep else WIDE[:1]
    for step, pose in enumerate(poses):
        subject.step_index = step
        box = detection(pose)
        current = subject.update_tracks(
            [box],
            pose_ned=pose,
            yaw_deg=0,
            ground_point=lambda u, v: GROUND,
            camera_ray=lambda u, v, pose=pose, box=box: (
                pose,
                (0.0, 0.0, 1.0) if v == box.bbox[3] else CAMERA.ray_ned(u, v, 0),
            ),
        )
    track = current[0]
    assert track.ground == GROUND
    if multistep:
        assert track.position_estimate.source == "triangulation"
        assert subject.target_position(track) == pytest.approx(TARGET[:2])
    else:
        assert track.position_estimate is None
        assert subject.target_position(track) == GROUND


def test_count_deduplicates_estimated_positions_even_when_ground_anchors_differ():
    subject = loop(
        step_triangulation=True,
        ground_height_sigma_m=20,
        stop_on_confirm=False,
        ground_reid_max_steps=0,
    )
    for start, anchor in ((0, (34.0, 0.0)), (10, (39.0, 0.0))):
        for offset, pose in enumerate(WIDE):
            subject.step_index = start + offset
            current = subject.update_tracks(
                [detection(pose)],
                pose_ned=pose,
                yaw_deg=0,
                ground_point=lambda u, v, anchor=anchor: anchor,
                camera_ray=lambda u, v, pose=pose: (pose, CAMERA.ray_ned(u, v, 0)),
            )
            _, info = act(subject, current, pose, anchor)
    assert set(subject.tracks) == {"t1", "t2"}
    assert (
        math.dist(subject.tracks["t1"].ground, subject.tracks["t2"].ground)
        > subject.config.count_dedupe_m
    )
    assert len(subject.confirmations) == 1
    assert info["duplicate_of_counted"] == "t2"
    assert subject.confirmations[0]["position_source"] == "fused"


@pytest.mark.parametrize(
    "options",
    [
        {"step_triangulation": "false"},
        {"step_triangulation": 1},
        {"step_triangulation_max_views": True},
        {"step_triangulation_max_views": 2.5},
        {"step_triangulation_max_views": 1},
        {"step_triangulation_max_views": 65},
        {"ground_height_sigma_m": True},
        {"ground_height_sigma_m": "1"},
        {"ground_height_sigma_m": 0},
        {"ground_height_sigma_m": float("nan")},
        {"step_triangulation_sigma_scale": float("inf")},
        {"step_triangulation_sigma_scale": 0.5},
        {"step_triangulation_ground_gate_m": -1},
        {"step_triangulation_ground_gate_m": float("nan")},
        {"step_triangulation_ground_gate_sigma": -0.5},
        {"step_triangulation_ground_gate_sigma": float("inf")},
        {"step_triangulation_ground_gate_sigma": True},
        {"step_triangulation_parallax_deg_per_m": 0},
        {"step_triangulation_parallax_deg_per_m": float("inf")},
        {"step_triangulation_min_ray_angle_deg": -1},
        {"step_triangulation_min_ray_angle_deg": float("nan")},
    ],
)
def test_new_configuration_is_strict(options):
    with pytest.raises(ValueError):
        LoopConfig(**options)


def test_a_tall_narrow_object_is_not_moved_by_its_height():
    # "person" has a typical size of its height; its box is narrow on the ground.
    camera = CameraModel(500, 500, 1000, 400, 2000, 800)
    subject = OnboardLoop(
        camera,
        lambda p, y: ActionPrimitive.hold(1),
        segment_clear=lambda *args: True,
        config=LoopConfig(step_triangulation=True, ground_extent_size_m=1.8),
    )
    pose = (0.0, 0.0, -10.0)
    track = subject.update_tracks(
        [Detection((990.0, 300.0, 1010.0, 500.0), 0.9)],  # 20 px wide: about 0.4 m
        pose_ned=pose,
        yaw_deg=0.0,
        ground_point=lambda u, v: (10.0, 0.0),
        camera_ray=lambda u, v: (pose, (0.8944271909999159, 0.0, 0.4472135954999579)),
    )[0]
    assert track.position_estimate.position_ned[:2] == pytest.approx((10.0, 0.0))
