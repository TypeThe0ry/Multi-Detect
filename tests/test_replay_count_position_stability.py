from __future__ import annotations

from scripts.replay_count_position_stability import _event_analysis


def test_count_replay_marks_position_miss_as_duplicate_of_prior_member() -> None:
    members = [
        {
            "target_id": "count_member_0000_01",
            "class": "traffic_cone",
            "label": "traffic cone cone",
            "count_member": True,
            "pose": {"y_north_m": 19.771, "x_east_m": 23.886},
        },
        {
            "target_id": "count_member_0000_02",
            "class": "traffic_cone",
            "label": "traffic cone cone",
            "count_member": True,
            "pose": {"y_north_m": -21.995, "x_east_m": 28.631},
        },
    ]
    prior = {
        "track_id": "t2",
        "position_ne": [19.84870778684079, 23.727590569236547],
        "analysis": {
            "nearest_truth_member": {"target_id": "count_member_0000_01"},
        },
    }
    result = _event_analysis(
        [18.76803272133463, 18.3225129542529],
        "t18",
        [prior],
        members,
        {},
        4.0,
    )

    assert result["nearest_truth_member"]["target_id"] == "count_member_0000_01"
    assert result["nearest_truth_member"]["class"] == "traffic_cone"
    assert result["duplicate_of_prior_track"] == "t2"
    assert result["nearest_prior_confirmation"]["distance_m"] > 4.0
    assert result["would_merge_by_position"] is False
    assert result["co_visibility_proves_distinct"] is False
