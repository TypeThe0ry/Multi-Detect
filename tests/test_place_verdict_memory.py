from __future__ import annotations

import math

import pytest

from multidetect.autonomy.onboard_loop import (
    CameraModel,
    Detection,
    LoopConfig,
    OnboardLoop,
    PlaceVerdictMemory,
)
from multidetect.sim_teacher.primitives import ActionPrimitive

CAMERA = CameraModel(fx=540.0, fy=540.0, cx=640.0, cy=480.0, width=1280, height=960)
POSE = (0.0, 0.0, -20.0)
POINT = (40.0, 0.0)


def _loop(**config) -> OnboardLoop:
    return OnboardLoop(
        camera=CAMERA,
        search_action=lambda pose, yaw: ActionPrimitive.translate(10.0, 0.0, 0.0),
        segment_clear=lambda pose, n, e: True,
        config=LoopConfig(**config),
    )


def _fragment(loop: OnboardLoop, point=POINT, pose=POSE):
    # Model the recorded failure mode: association lost the old track, then FAST
    # proposed a new ID at the same physical location.
    loop._tracks.clear()
    return loop.update_tracks(
        [Detection((630.0, 690.0, 650.0, 710.0), 0.5)],
        pose_ned=pose,
        yaw_deg=0.0,
        ground_point=lambda u, v: point,
    )[0]


def _train(loop: OnboardLoop, decision: str, count: int = 3) -> None:
    for _ in range(count):
        track = _fragment(loop)
        loop.apply_verdict(track.track_id, decision)
        loop.step_index += 1


def test_memory_is_off_by_default() -> None:
    loop = _loop()
    assert loop.config.memory_enabled is False
    _train(loop, "reject_track", 5)
    track = _fragment(loop)
    assert loop.remembered_verdict(track.track_id) is None
    assert loop._place_memory.places == []


@pytest.mark.parametrize("decision", ["reject_track", "approach_track"])
def test_reuses_only_after_k_real_agreeing_verdicts_across_fragmented_ids(decision) -> None:
    loop = _loop(memory_enabled=True)
    _train(loop, decision, 2)
    third = _fragment(loop, point=(42.0, 0.0))
    assert loop.remembered_verdict(third.track_id) is None
    loop.apply_verdict(third.track_id, decision)
    fourth = _fragment(loop, point=(41.0, 1.0))
    remembered = loop.remembered_verdict(fourth.track_id)
    assert remembered is not None
    assert remembered["decision"] == decision
    assert remembered["remembered"] is True
    assert remembered["source"] == "place_verdict_memory"
    assert remembered["latency_s"] == 0.0
    assert fourth.verdict is None  # Lookup does not silently apply its result.
    assert fourth.track_id == "t4"
    loop.apply_verdict(fourth.track_id, remembered["decision"], remembered=True)
    assert fourth.verdict_remembered is True
    assert fourth.match_steps == []
    assert fourth.confirm_steps == []
    assert fourth.identity_verdict_history == [
        "approach" if decision == "approach_track" else "no_match"
    ]
    assert len(loop._place_memory.places[0].verdicts) == 3


def test_configurable_minimum_and_cluster_radius() -> None:
    loop = _loop(memory_enabled=True, memory_min_verdicts=2, memory_radius_m=1.0)
    _train(loop, "reject_track", 2)
    nearby = _fragment(loop, point=(40.9, 0.0))
    assert loop.remembered_verdict(nearby.track_id) is not None
    outside = _fragment(loop, point=(41.1, 0.0))
    assert loop.remembered_verdict(outside.track_id) is None


@pytest.mark.parametrize("conflict", ["approach_track", "confirm_target", None, "unknown"])
def test_disagreement_or_unknown_keeps_place_ineligible(conflict) -> None:
    loop = _loop(memory_enabled=True)
    _train(loop, "reject_track")
    track = _fragment(loop)
    loop.apply_verdict(track.track_id, conflict)
    _train(loop, "reject_track", 5)
    track = _fragment(loop)
    assert loop.remembered_verdict(track.track_id) is None


def test_closer_look_uses_closest_real_range_and_configured_ratio() -> None:
    memory = PlaceVerdictMemory()
    config = LoopConfig(memory_enabled=True)
    for distance in (100.0, 80.0, 60.0):
        memory.record(POINT, distance, "reject_track", config)
    assert memory.lookup(POINT, 42.0, config) == "reject_track"
    assert memory.lookup(POINT, 41.9, config) is None
    memory.record(POINT, 40.0, "reject_track", config)
    assert memory.lookup(POINT, 28.0, config) == "reject_track"
    assert memory.lookup(POINT, 27.9, config) is None
    stricter = LoopConfig(memory_enabled=True, memory_max_range_ratio=0.9)
    assert memory.lookup(POINT, 35.0, stricter) is None


def test_synthetic_looks_do_not_train_memory_or_move_closest_real_range() -> None:
    loop = _loop(memory_enabled=True)
    _train(loop, "approach_track")
    candidate = _fragment(loop, pose=(12.0, 0.0, -20.0))  # 28 m, exactly 70%.
    result = loop.remembered_verdict(candidate.track_id)
    assert result is not None
    loop.apply_verdict(candidate.track_id, result["decision"], remembered=True)
    closer = _fragment(loop, pose=(15.0, 0.0, -20.0))
    assert loop.remembered_verdict(closer.track_id) is None
    assert loop._place_memory.places[0].closest_range_m == 40.0
    assert len(loop._place_memory.places[0].verdicts) == 3


@pytest.mark.parametrize("stop_on_confirm", [True, False], ids=["find", "count"])
@pytest.mark.parametrize("range_limit", [None, 10.0], ids=["confirm", "downgraded_confirm"])
def test_never_reuses_confirm_even_when_host_downgraded_it(stop_on_confirm, range_limit) -> None:
    loop = _loop(
        memory_enabled=True,
        stop_on_confirm=stop_on_confirm,
        confirm_max_range_m=range_limit,
    )
    _train(loop, "confirm_target")
    track = _fragment(loop)
    assert loop.remembered_verdict(track.track_id) is None
    with pytest.raises(ValueError, match="only reject_track and approach_track"):
        loop.apply_verdict(track.track_id, "confirm_target", remembered=True)


@pytest.mark.parametrize("stop_on_confirm", [True, False], ids=["find", "count"])
def test_remembered_approach_requires_fresh_terminal_look(stop_on_confirm) -> None:
    loop = _loop(
        memory_enabled=True,
        stop_on_confirm=stop_on_confirm,
        closest_view_identity_matches=1,
    )
    _train(loop, "approach_track")
    track = _fragment(loop)
    remembered = loop.remembered_verdict(track.track_id)
    assert remembered is not None
    loop.apply_verdict(track.track_id, remembered["decision"], remembered=True)
    # Even prior genuine identity votes must not let the current remembered
    # judgment terminate find or increment count when the drone reaches it.
    track.match_steps.append(loop.step_index - 1)
    near_pose = (35.0, 0.0, -20.0)
    _, info = loop.next_action(
        [track], pose_ned=near_pose, yaw_deg=0.0, ground_point=lambda u, v: POINT
    )
    assert "confirmed" not in info
    assert loop.confirmed is None
    assert loop.confirmations == []
    assert loop.state == "approaching"
    assert loop.remembered_verdict(track.track_id) is None  # Active focus gets a real look.
    loop.apply_verdict(track.track_id, "approach_track")
    assert track.verdict_remembered is False
    _, info = loop.next_action(
        [track], pose_ned=near_pose, yaw_deg=0.0, ground_point=lambda u, v: POINT
    )
    assert ("confirmed" if stop_on_confirm else "counted") in info
    assert len(loop.confirmations) == (0 if stop_on_confirm else 1)
    assert (loop.confirmed is not None) == stop_on_confirm


def test_second_look_bypasses_a_remembered_rejection() -> None:
    loop = _loop(memory_enabled=True)
    _train(loop, "reject_track")
    track = _fragment(loop)
    assert loop.remembered_verdict(track.track_id) is not None
    loop._revisit = {"ground": POINT}
    assert loop.remembered_verdict(track.track_id) is None


def test_memory_is_scoped_to_one_loop_task() -> None:
    first = _loop(memory_enabled=True)
    second = _loop(memory_enabled=True)
    _train(first, "reject_track")
    assert first.remembered_verdict(_fragment(first).track_id) is not None
    assert second.remembered_verdict(_fragment(second).track_id) is None


@pytest.mark.parametrize(
    ("point", "distance"),
    [
        (None, 40.0),
        ((math.nan, 0.0), 40.0),
        ((0.0, math.inf), 40.0),
        (POINT, None),
        (POINT, math.nan),
        (POINT, math.inf),
        (POINT, -1.0),
    ],
)
def test_unknown_or_invalid_positions_and_ranges_are_not_cached(point, distance) -> None:
    memory = PlaceVerdictMemory()
    config = LoopConfig(memory_enabled=True)
    for _ in range(3):
        memory.record(point, distance, "reject_track", config)
    assert memory.places == []
    assert memory.lookup(point, distance, config) is None


def test_loop_without_ground_or_pose_cannot_reuse_a_verdict() -> None:
    loop = _loop(memory_enabled=True)
    _train(loop, "reject_track")
    track = _fragment(loop, point=None)
    assert loop.remembered_verdict(track.track_id) is None
    track.ground = POINT
    loop._last_pose = None
    assert loop.remembered_verdict(track.track_id) is None
    assert loop.remembered_verdict("missing-track") is None
