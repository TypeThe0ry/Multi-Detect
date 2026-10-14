from __future__ import annotations

import json
import tempfile
from pathlib import Path

from multidetect.simulator.task_semantics import task_equivalent


def test_second_person_satisfies_find_the_person_but_other_kinds_and_colours_do_not() -> None:
    target = {"category_words": ["person", "pedestrian"], "color": None}
    assert task_equivalent(target, {"category_words": ["person", "woman"], "color": None})
    assert not task_equivalent(target, {"category_words": ["postbox"], "color": None})
    blue_car = {"category_words": ["car", "hatchback"], "color": "blue"}
    assert not task_equivalent(blue_car, {"category_words": ["car", "hatchback"], "color": "red"})
    assert task_equivalent(blue_car, {"category_words": ["car", "suv"], "color": "blue"})
    assert not task_equivalent(blue_car, {"category_words": ["car", "suv"], "color": "red"})
    fire_truck = {"category_words": ["fire truck", "truck"], "color": "red"}
    assert not task_equivalent(fire_truck, {"category_words": ["pickup truck"], "color": None})


def test_unknown_colour_cannot_rule_out_and_category_match_counts() -> None:
    blue_car = {"category_words": ["car", "hatchback"], "color": "blue", "class": "car"}
    assert task_equivalent(blue_car, {"category_words": ["car", "suv"], "color": None})
    person = {"category_words": ["person", "pedestrian"], "class": "person"}
    assert task_equivalent(person, {"category_words": ["woman"], "class": "person"})


def test_count_metrics_match_findings_to_distinct_members() -> None:
    from scripts.score_onboard_loop import _count_metrics

    members = [
        {"pose": {"y_north_m": 0.0, "x_east_m": 0.0}},
        {"pose": {"y_north_m": 50.0, "x_east_m": 0.0}},
        {"pose": {"y_north_m": 0.0, "x_east_m": 50.0}},
    ]
    rows = [
        {
            "labels": {
                "onboard": {
                    "mission": {
                        "counts": {"count": 3},
                        "positions": ["1.0,0.0", "0.5,0.5", "49.0,1.0"],
                    }
                }
            }
        }
    ]
    metrics = _count_metrics(rows, members, 10.0)
    # Two findings near the same member: only one can match it.
    assert metrics["matched_count"] == 2 and metrics["false_findings"] == 1
    assert metrics["reported_count"] == 3 and metrics["count_exact"] is False


def test_mission_metrics_score_alerts_coverage_inspect_and_completion() -> None:
    from scripts.score_onboard_loop import _mission_metrics

    truth = {
        "targets": [
            {
                "label": "backpack bag",
                "category_words": ["backpack", "bag"],
                "pose": {"y_north_m": 0.0, "x_east_m": 0.0},
                "true_target": True,
            },
            {
                "label": "backpack bag",
                "category_words": ["backpack", "bag"],
                "pose": {"y_north_m": 20.0, "x_east_m": 0.0},
            },
            {
                "label": "postbox",
                "category_words": ["postbox"],
                "pose": {"y_north_m": 1.0, "x_east_m": 0.0},
            },
            {
                "label": "backpack outside zone",
                "category_words": ["backpack", "bag"],
                "pose": {"y_north_m": 80.0, "x_east_m": 0.0},
            },
        ]
    }
    rows = [
        {
            "labels": {
                "onboard": {
                    "mission": {"plan": {"skills": ["monitor", "survey", "inspect", "return_home"]}}
                }
            }
        },
        {
            "labels": {
                "onboard": {
                    "mission": {
                        "history": [{"step": "home", "skill": "return_home", "state": "succeeded"}],
                        "alerts": [
                            "watch:alert:backpack@0.0,0.0",
                            "watch:alert:backpack@1.0,0.0",
                        ],
                        "answers": [
                            "survey-area:coverage:0.75",
                            "inspect-target:view1:confirm_target",
                            "inspect-target:view2:approach_track",
                            "inspect-target:view3:reject",
                        ],
                    }
                }
            }
        },
    ]

    metrics = _mission_metrics(
        rows,
        truth,
        3.0,
        mission_config={"refs": {"zone:site": [0.0, 0.0, 30.0]}},
    )

    assert metrics["mission_plan_skills"] == ["monitor", "survey", "inspect", "return_home"]
    assert metrics["mission_alerts"][0]["matches_query"] is True
    assert metrics["mission_alerts"][1]["matches_query"] is False
    assert metrics["mission_alert_precision"] == 0.5
    assert metrics["mission_alert_recall"] == 0.5
    assert metrics["mission_survey_coverage"] == {"survey-area": 0.75}
    assert [answer["matches"] for answer in metrics["mission_inspect_answers"]] == [
        True,
        True,
        False,
    ]
    assert metrics["mission_inspect_match_fraction"] == 2 / 3
    assert metrics["mission_completed"] is True


def test_mission_metrics_without_mission_labels_preserve_existing_find_and_count_keys() -> None:
    from scripts.score_onboard_loop import score_episode

    with tempfile.TemporaryDirectory(prefix=".score-onboard-loop-", dir=Path.cwd()) as directory:
        episode = Path(directory)
        (episode / "episode.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
        (episode / "steps.jsonl").write_text(
            json.dumps(
                {
                    "labels": {"onboard": {"state_before": "searching"}},
                    "obs_before": {"pose": {"local_ned_m": [0.0, 0.0, -10.0]}},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        truth = {
            "targets": [
                {
                    "target_id": "target",
                    "true_target": True,
                    "count_member": True,
                    "pose": {
                        "y_north_m": 0.0,
                        "x_east_m": 0.0,
                        "z_up_m": 0.0,
                        "yaw_rad": 0.0,
                    },
                    "size_m": [2.0, 2.0, 2.0],
                }
            ]
        }

        result = score_episode(episode, truth)

        assert result["confirmed"] is False
        assert result["success"] is False
        assert result["expected_count"] == 1
        assert result["reported_count"] is None
        assert result["matched_count"] == 0
        assert result["count_exact"] is False
        assert not any(key.startswith("mission_") for key in result)
