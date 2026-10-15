from __future__ import annotations

import copy

import pytest

from scripts.replay_track_association import replay_rows


def _row(track_id=None, *, task="s1", skill="monitor", box=None):
    tracks = [] if track_id is None else [{
        "id": track_id, "bbox": box or [620.0, 600.0, 660.0, 640.0], "verdict": "no_match",
    }]
    return {
        "obs_before": {
            "pose": {"local_ned_m": [0.0, 0.0, -15.0]},
            "attitude": {"yaw_deg": 0.0, "pitch_deg": 0.0},
            "camera_info": {"fx": 540, "fy": 540, "cx": 640, "cy": 480,
                            "width": 1280, "height": 960},
        },
        "labels": {"onboard": {
            "step": task, "skill": skill, "tracks": tracks, "detections": len(tracks),
            "slow_calls": [] if not tracks else [{"track_id": track_id}],
        }},
    }


def test_replay_measures_identity_and_ttl_cost_without_changing_inputs():
    rows = [_row("t1")] + [_row() for _ in range(7)] + [_row("t2")]
    original = copy.deepcopy(rows)
    result = replay_rows(rows)
    assert rows == original
    assert result["causes"] == {"lost_within_gate": 1}
    assert result["old_ids"] == 2 and result["new_ids"] == 1
    assert result["old_slow_candidates"] == 2 and result["new_slow_candidates"] == 1
    assert result["baseline_exact_id_boxes"] == 2
    cluster = result["clusters"][0]
    assert cluster["recorded_ids"] == cluster["old_ids"] == ["t1", "t2"]
    assert cluster["new_ids"] == ["t1"] and cluster["recorded_slow_calls"] == 2
    expired = replay_rows(rows, reid_steps=7)
    assert expired["new_ids"] == 2
    ttl_rows = [_row("t1")] + [_row() for _ in range(15)] + [_row("t2")]
    ttl = replay_rows(ttl_rows)
    assert ttl["new_ids"] == 1 and ttl["new_slow_candidates"] == 2
    assert ttl["new_first_look_candidates"] == 1


def test_replay_separates_tasks_and_keeps_find_association_unchanged():
    rows = [_row("t1"), _row("t1", task="s2")]
    result = replay_rows(rows)
    assert result["old_ids"] == result["new_ids"] == 2
    assert len(result["clusters"]) == 2 and not result["fragment_events"]
    rows = [_row("t1", skill="area_search")]
    rows += [_row(skill="area_search") for _ in range(7)]
    rows += [_row("t2", skill="area_search")]
    result = replay_rows(rows)
    assert result["old_ids"] == result["new_ids"] == 2


def test_replay_flags_clipped_reappearance_and_does_not_invent_ground():
    box = [620.0, 920.0, 660.0, 960.0]
    rows = [_row("t1", box=box)] + [_row() for _ in range(7)] + [_row("t2", box=box)]
    result = replay_rows(rows)
    assert result["cause_flags"]["edge_clipped"] == 1
    assert result["old_ids"] == result["new_ids"] == 2
    result = replay_rows([_row("t1", box=[620.0, 100.0, 660.0, 140.0])])
    assert result["unprojectable_boxes"] == 1 and result["clusters"] == []


def test_same_frame_multiple_boxes_are_not_labelled_timeout():
    row = _row("t1")
    row["labels"]["onboard"]["tracks"].append({
        "id": "t2", "bbox": [621.0, 600.0, 661.0, 640.0],
    })
    result = replay_rows([row])
    assert result["causes"] == {"same_frame_multiple_boxes": 1}
    assert result["old_ids"] == result["new_ids"] == 2


@pytest.mark.parametrize("radius", [0, -1, float("nan"), float("inf")])
def test_replay_rejects_invalid_cluster_radius(radius):
    with pytest.raises(ValueError, match="radius_m"):
        replay_rows([], radius_m=radius)
