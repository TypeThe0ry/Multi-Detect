from __future__ import annotations

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
