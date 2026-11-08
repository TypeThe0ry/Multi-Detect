"""Replay measured rays from the real four-count failure without a simulator."""
import json
import math
from pathlib import Path

import pytest

from multidetect.autonomy.onboard_loop import AppearanceSignature, CameraModel, LoopConfig, OnboardLoop, Track
from multidetect.autonomy.step_triangulation import (
    ViewObservation, fuse_position_estimates, make_ground_estimate, triangulate_views,
)
from multidetect.sim_teacher.primitives import ActionPrimitive
from multidetect.simulator.camera_projection import pixel_camera_ray
from scripts.evaluate_onboard_ranging_reuse import triangulation_quality

FIXTURE = Path(__file__).parent / "fixtures/count_cones_yaw_geometry_6c20ce66.json"
CONE = AppearanceSignature(size_range=0.02, aspect_ratio=0.5, footprint_m=0.5)


def estimates():
    data = json.loads(FIXTURE.read_text())
    views = []
    for sample in data["measurements"]:
        observation, box = sample["observation"], sample["bbox"]
        centre = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        kwargs = dict(pose=observation, camera_info=observation["camera_info"], image_size=(1280, 960))
        camera, direction = pixel_camera_ray(*centre, **kwargs)
        views.append(ViewObservation(camera, direction, sample["step"],
                                     observation["frame_sensor_stamp_ns"] / 1e9))
    camera, bottom = pixel_camera_ray(centre[0], box[3], **kwargs)
    ground = make_ground_estimate(
        (*sample["ground_ne"], 0.0), camera_ned=camera, direction_ned=bottom,
        ground_height_sigma_m=5.0, timestamp_s=views[-1].timestamp_s,
    )
    tri = triangulate_views(views)
    # The existing configured ground-height uncertainty gate, unchanged.
    gate = 2.5 * 5.0 * math.hypot(*bottom[:2]) / bottom[2]
    return data, ground, tri, views, gate


def subject_with_prior_findings(data):
    subject = OnboardLoop(CameraModel(500, 500, 500, 400, 1000, 800),
                          lambda *args: ActionPrimitive.hold(1),
                          config=LoopConfig(stop_on_confirm=False))
    for original in data["prior_confirmations"]:
        track = Track(original["track_id"], tuple(original["bbox"]), 0, 0)
        track.appearance = CONE
        subject._count_confirmation(track, dict(original), (0, 0, -10), 0, {})
    assert len(subject.confirmations) == 3
    return subject


def test_original_four_ray_failure_no_longer_manufactures_a_fourth_object():
    data, ground, tri, views, gate = estimates()
    assert tri is not None and ground is not None
    assert tri.sigma_m > 10.0  # Original residual-only sigma was 2.667 m.
    quality = triangulation_quality([(v.camera_ned, v.direction_ned) for v in views], tri.position_ned)
    assert quality[-1] * 2.5 == pytest.approx(tri.sigma_m)
    fused = fuse_position_estimates(ground, tri, ground_anchor_max_error_m=gate)
    assert fused is not None and fused.source == "fused"
    prior = data["prior_confirmations"][1]["position_estimate_ne"]
    original = data["original_fourth_confirmation"]
    assert math.dist(original["position_estimate_ne"], prior) > 8.0
    assert math.dist(fused.position_ned[:2], prior) < 4.0

    # Preserve and demonstrate the actual failure with its original output.
    legacy = subject_with_prior_findings(data)
    track = Track("t21", tuple(original["bbox"]), 54, 57, ground=tuple(data["measurements"][-1]["ground_ne"]))
    track.appearance = CONE
    legacy._count_confirmation(track, dict(original), (0, 0, -10), 0, {})
    assert len(legacy.confirmations) == 4

    corrected = subject_with_prior_findings(data)
    _, info = corrected._count_confirmation(
        track, {**original, "position_estimate_ne": list(fused.position_ned[:2]),
                "position_sigma_m": fused.sigma_m}, (0, 0, -10), 0, {},
    )
    assert len(corrected.confirmations) == 3
    assert info["duplicate_of_counted"] == "t21"
    assert corrected.config.count_dedupe_m == 4.0


def test_large_position_uncertainty_does_not_merge_real_covisible_neighbours():
    data, ground, tri, _, gate = estimates()
    fused = fuse_position_estimates(ground, tri, ground_anchor_max_error_m=gate)
    subject = subject_with_prior_findings(data)
    first = subject.confirmations[1]
    neighbour = Track("independent-cone", (900, 400, 930, 460), 57, 57,
                      ground=(first["position_estimate_ne"][0], first["position_estimate_ne"][1] + 1.0))
    neighbour.appearance = CONE
    neighbour.co_visible_with.add(first["track_id"])
    neighbour.co_visible_geometry[first["track_id"]] = (
        57, neighbour.ground, tuple(first["position_estimate_ne"]),
    )
    _, info = subject._count_confirmation(
        neighbour, {"position_estimate_ne": list(neighbour.ground), "position_sigma_m": fused.sigma_m},
        (0, 0, -10), 0, {},
    )
    assert "counted" in info and len(subject.confirmations) == 4


def test_well_conditioned_nonzero_residual_keeps_the_previous_sigma():
    views = [ViewObservation((-10, 0, 0), (1, 0, 0), 0, 0),
             ViewObservation((0, -10, 2), (0, 1, 0), 1, 1),
             ViewObservation((2, 0, -10), (0, 0, 1), 2, 2)]
    estimate = triangulate_views(views)
    assert estimate is not None and estimate.residual_m > 0.5
    old_sigma = 2.5 * max(0.5, estimate.residual_m + 0.003 * math.dist(
        estimate.position_ned, views[-1].camera_ned,
    ) / max(math.sin(math.radians(estimate.ray_angle_deg)), 0.05))
    assert estimate.sigma_m == pytest.approx(old_sigma)


def test_offline_quality_returns_no_result_for_singular_rays():
    samples = [((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
               ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0))]
    assert triangulation_quality(samples, (10.0, 0.5, 0.0)) is None
