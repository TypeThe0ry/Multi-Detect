from __future__ import annotations

import math

import pytest

from multidetect.autonomy.onboard_loop import (
    AppearanceSignature,
    CameraModel,
    Detection,
    LoopConfig,
    OnboardLoop,
    appearances_consistent,
)
from multidetect.sim_teacher.primitives import ActionPrimitive

CAMERA = CameraModel(540, 540, 640, 480, 1280, 960)
POSE = (0.0, 0.0, -15.0)
BOX = (620.0, 600.0, 660.0, 640.0)
OTHER_BOX = (900.0, 600.0, 940.0, 640.0)


def _loop(**options):
    return OnboardLoop(
        CAMERA, lambda pose, yaw: ActionPrimitive.hold(1), lambda pose, n, e: True,
        LoopConfig(**options),
    )


def _observe(loop, step, point=(30.0, 10.0), box=BOX, **options):
    loop.step_index = step
    return loop.update_tracks(
        [Detection(box)], pose_ned=POSE, yaw_deg=options.pop("yaw", 0.0),
        ground_point=lambda u, v: point, **options,
    )[0]


def test_continuing_loop_revives_identity_evidence_and_verdict_age_after_scan():
    loop = _loop(stop_on_confirm=False)
    original = _observe(loop, 1)
    loop.apply_verdict(original.track_id, "approach_track")
    original.confirm_steps.append(1)
    revived = _observe(loop, 9, (30.4, 10.2), OTHER_BOX, yaw=170)
    assert revived is original
    assert revived.first_step == 1 and revived.last_step == 9 and revived.hits == 2
    assert revived.identity_positive_matches == 1
    assert revived.identity_verdict_history == ["approach"]
    assert revived.match_steps == [] and revived.confirm_steps == []
    assert revived.needs_fresh_verdict and revived.verdict_step == -1
    assert loop.tracks_needing_verdict([revived]) == [revived]


def test_soft_appearance_gate_accepts_rotation_and_partial_occlusion():
    config = LoopConfig()
    first = AppearanceSignature(120.0, 2.0, 1.0, (1.0,) + (0.0,) * 71, 48.0)
    turned = AppearanceSignature(165.0, 0.5, 1.1, (1.0,) + (0.0,) * 71, 25.0)
    assert appearances_consistent(first, turned, config)


def test_soft_appearance_gate_rejects_different_colour_same_shape():
    config = LoopConfig()
    first = AppearanceSignature(120.0, 2.0, 1.0, (1.0,) + (0.0,) * 71, 48.0)
    neighbour = AppearanceSignature(120.0, 2.0, 1.0, (0.0, 1.0) + (0.0,) * 70, 48.0)
    assert not appearances_consistent(first, neighbour, config)


def test_exhausted_track_is_not_active_association_candidate():
    loop = _loop(stop_on_confirm=False)
    original = _observe(loop, 0)
    original.exhausted = True
    replacement = _observe(loop, 8, (30.0, 3.0), OTHER_BOX)
    assert replacement is not original
    assert replacement.track_id != original.track_id


@pytest.mark.parametrize("options", [{}, {"stop_on_confirm": False, "ground_reid_max_steps": 0}])
def test_find_default_and_explicit_disable_keep_old_lost_timeout(options):
    loop = _loop(**options)
    original = _observe(loop, 0)
    assert _observe(loop, 5).track_id != original.track_id


@pytest.mark.parametrize("age, same", [(4, True), (5, True), (60, True), (61, False)])
def test_reid_age_is_bounded_and_does_not_extend_active_timeout(age, same):
    loop = _loop(stop_on_confirm=False)
    original = _observe(loop, 0)
    assert (_observe(loop, age) is original) is same


@pytest.mark.parametrize(
    "distance, same", [(1.499, True), (1.5, False), (3.0, False), (10.0, False)]
)
def test_lost_reid_uses_strict_footprint_radius(distance, same):
    loop = _loop(stop_on_confirm=False, ground_reid_footprint_factor=0.0)
    original = _observe(loop, 0, (300.0, 0.0))
    assert (_observe(loop, 8, (300.0 + distance, 0.0)) is original) is same


@pytest.mark.parametrize("box", [
    (0.0, 600.0, 40.0, 640.0), (1240.0, 600.0, 1280.0, 640.0),
    (620.0, 0.0, 660.0, 40.0), (620.0, 920.0, 660.0, 960.0),
])
@pytest.mark.parametrize("clipped_first", [True, False])
def test_clipped_anchors_cannot_revive_lost_tracks(box, clipped_first):
    loop = _loop(stop_on_confirm=False)
    original = _observe(loop, 0, box=box if clipped_first else BOX)
    revived = _observe(loop, 8, box=OTHER_BOX if clipped_first else box)
    assert revived is not original


@pytest.mark.parametrize("point", [None, (math.nan, 1.0), (1.0, math.inf)])
def test_missing_or_nonfinite_ground_never_uses_stale_pixel_fallback(point):
    loop = _loop(stop_on_confirm=False)
    original = _observe(loop, 0)
    assert _observe(loop, 8, point=point) is not original


def test_active_match_wins_and_lost_track_cannot_take_second_detection():
    loop = _loop(stop_on_confirm=False)
    lost = _observe(loop, 0)
    active = _observe(loop, 10, (40.0, 10.0), OTHER_BOX)
    loop.step_index = 11
    current = loop.update_tracks(
        [Detection(BOX), Detection(OTHER_BOX)], pose_ned=POSE, yaw_deg=0,
        ground_point=lambda u, v: (30.0, 10.0),
    )
    # The second box has a valid active pixel match. Revival is a second pass
    # after all active associations, with one detection per lost track.
    assert current[1] is active
    assert current[0] is lost
    assert len({t.track_id for t in current}) == 2


def test_reid_uses_nearest_pairs_once_and_keeps_neighbouring_objects_separate():
    loop = _loop(stop_on_confirm=False)
    loop.step_index = 0
    first = loop.update_tracks(
        [Detection(BOX), Detection(OTHER_BOX)], pose_ned=POSE, yaw_deg=0,
        ground_point=lambda u, v: (30.0, 0.0 if u < 800 else 4.0),
    )
    loop.step_index = 8
    current = loop.update_tracks(
        [Detection(OTHER_BOX), Detection(BOX)], pose_ned=POSE, yaw_deg=170,
        ground_point=lambda u, v: (30.0, 3.5 if u > 800 else 0.5),
    )
    assert current == [first[1], first[0]]
    loop.step_index = 16
    current = loop.update_tracks(
        [Detection(BOX), Detection(OTHER_BOX)], pose_ned=POSE, yaw_deg=0,
        ground_point=lambda u, v: (30.0, 0.0),
    )
    assert current[0] is first[0]
    assert current[1].track_id not in {t.track_id for t in first}


def test_covisibility_eviction_marks_tracks_and_blocks_revival():
    loop = _loop(stop_on_confirm=False)
    loop.co_visibility_max_pairs = 1
    loop.step_index = 0
    first = loop.update_tracks(
        [Detection(BOX), Detection(OTHER_BOX)], pose_ned=POSE, yaw_deg=0,
        ground_point=lambda u, v: (30.0, 0.0 if u < 800 else 8.0),
    )
    assert len(loop._co_visibility_pairs) == 1
    loop.step_index = 8
    loop.update_tracks(
        [Detection((100.0, 100.0, 140.0, 140.0)), Detection((300.0, 100.0, 340.0, 140.0))],
        pose_ned=POSE, yaw_deg=0,
        ground_point=lambda u, v: (60.0, 20.0 if u < 200 else 28.0),
    )
    assert first[0].covis_truncated and first[1].covis_truncated
    loop.step_index = 16
    replacement = loop.update_tracks(
        [Detection(BOX)], pose_ned=POSE, yaw_deg=0,
        ground_point=lambda u, v: (30.1, 0.0),
    )[0]
    assert replacement is not first[0]


@pytest.mark.parametrize("options", [
    {"ground_reid_max_steps": -1}, {"ground_reid_max_steps": 1.5},
    {"ground_reid_max_steps": True}, {"ground_reid_radius_m": 0},
    {"ground_reid_radius_m": math.inf}, {"ground_reid_radius_m": math.nan},
])
def test_invalid_reid_configuration_is_rejected(options):
    with pytest.raises(ValueError, match="ground_reid"):
        LoopConfig(**options)
