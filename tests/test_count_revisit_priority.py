from types import SimpleNamespace

from multidetect.autonomy.mission_executor import MissionExecutor
from multidetect.autonomy.onboard_loop import LoopConfig, Track


def _loop(tracks):
    return SimpleNamespace(
        _tracks={track.track_id: track for track in tracks},
        config=LoopConfig(small_unjudged_query="fire hydrant"),
    )


def test_hydrant10_rejection_remains_pending_despite_covisibility():
    # SITL 7166ed2b h0 and counted h1 appeared in one frame but are distinct places.
    loop = _loop([Track("h0", (0, 0, 14, 14), 1, 1, label="fire hydrant")])
    item = {
        "track_id": "h0", "ground": (8.76, -15.02), "min_side_px": [8.0],
        "co_visible_with": {"h1"},
    }
    run = SimpleNamespace(loop=loop, step=SimpleNamespace(), rejection_revisit_places=[],
                          revisited_places=[], small_unjudged_revisits=[])
    loop.rejected_far_small_places = lambda _range: [item]
    loop.confirmations = [{"track_id": "h1", "position_estimate_ne": [30.0, 0.0],
                           "co_visible_with": {"h0"}}]
    executor = MissionExecutor.__new__(MissionExecutor)
    executor.config = SimpleNamespace(count_rejection_revisit_range_m=15.0)
    executor._in_step_area = lambda _run, _point: True
    assert executor._pending_rejection_places(run) == [item]


def test_lookgeometry_hydrant_priority_beats_nearer_confusers():
    # SITL 3121ac38: target third by distance behind these two confuser places.
    distractor_a = Track("d1", (0, 0, 12, 12), 1, 1, label="fire truck")
    distractor_b = Track("d2", (0, 0, 12, 12), 1, 1, label="building")
    target = Track("h2", (0, 0, 12, 12), 1, 1, label="fire hydrant",
                   identity_verdict_history=["approach"])
    loop = _loop([distractor_a, distractor_b, target])
    places = [
        {"track_id": "d1", "ground": (18.5, 21.4), "min_side_px": [18]},
        {"track_id": "d2", "ground": (2.4, 23.9), "min_side_px": [18]},
        {"track_id": "h2", "ground": (-15.6, -36.82), "min_side_px": [11]},
    ]
    ordered = sorted(places, key=lambda p: MissionExecutor._revisit_priority(loop, p, (0, 0)))
    assert ordered[0]["track_id"] == "h2"
