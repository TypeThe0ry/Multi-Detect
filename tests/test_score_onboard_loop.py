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


def test_loop_trajectory_position_uses_constant_speed_and_wraps() -> None:
    from multidetect.simulator.world_gen import trajectory_position
    from scripts.score_onboard_loop import target_pose_at_time

    trajectory = {
        "speed_mps": 2.0,
        "waypoints": [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]],
    }
    assert trajectory_position(trajectory, 0.0) == (0.0, 0.0)
    assert trajectory_position(trajectory, 2.0) == (4.0, 0.0)
    assert trajectory_position(trajectory, 4.0) == (4.0, 4.0)
    assert trajectory_position(trajectory, 8.0) == (0.0, 0.0)
    assert target_pose_at_time(
        {"pose": {"x_east_m": 0.0, "y_north_m": 0.0}, "trajectory": trajectory}, 1.0
    ) == {"x_east_m": 2.0, "y_north_m": 0.0}


def test_moving_track_metrics_use_sim_truth_at_each_observation() -> None:
    from scripts.score_onboard_loop import _moving_track_metrics

    target = {
        "pose": {"x_east_m": 0.0, "y_north_m": 0.0},
        "trajectory": {"speed_mps": 1.0, "waypoints": [[0, 0], [10, 0]]},
    }
    rows = [
        {
            "frame_sensor_stamp_ns": 0,
            "labels": {"onboard": {"confirmed": {"position_estimate_ne": [0, 0]}}},
        },
        {
            "frame_sensor_stamp_ns": 5_000_000_000,
            "labels": {"onboard": {"confirmed": {"position_estimate_ne": [0, 5]}}},
        },
    ]
    metrics = _moving_track_metrics(rows, target, 1.0, 5.0)
    assert metrics["moving_track_sample_count"] == 2
    assert metrics["moving_track_continuity"] == 1.0
    assert metrics["moving_distance_max_m"] == 0.0


def test_hovering_target_does_not_meet_time_coverage_or_mean_distance_gate() -> None:
    from scripts.score_onboard_loop import _moving_track_metrics

    target = {
        "pose": {"x_east_m": 0.0, "y_north_m": 0.0},
        "trajectory": {"speed_mps": 1.0, "waypoints": [[0, 0], [100, 0]]},
    }
    rows = [
        {
            "frame_sensor_stamp_ns": i * 1_000_000_000,
            "labels": {"onboard": {"confirmed": {"position_estimate_ne": [0, 0]}}},
        }
        for i in range(0, 91, 10)
    ]
    metrics = _moving_track_metrics(rows, target, 2.0, 120.0)
    assert metrics["moving_time_coverage"] < 0.8
    assert metrics["moving_track_eligible"] is False


def test_follow_metrics_report_visibility_loss_reacquire_and_false_follow() -> None:
    from scripts.score_onboard_loop import _moving_track_metrics

    target = {
        "target_id": "red-person",
        "pose": {"x_east_m": 0.0, "y_north_m": 0.0},
        "trajectory": {"speed_mps": 1.0, "waypoints": [[0, 0], [20, 0]]},
    }
    rows = []
    for index, (visible, target_id) in enumerate(
        [(True, "red-person"), (False, None), (True, "red-person"), (True, "blue-car")]
    ):
        confirmed = {"position_estimate_ne": [0.0, float(index)], "target_visible": visible}
        if target_id is not None:
            confirmed["target_id"] = target_id
        rows.append(
            {"frame_sensor_stamp_ns": index * 1_000_000_000,
             "labels": {"onboard": {"confirmed": confirmed}}}
        )
    metrics = _moving_track_metrics(rows, target, 2.0, 3.0)
    assert metrics["follow_target_in_frame_fraction"] == 0.75
    assert metrics["follow_lost_count"] == 1
    assert metrics["follow_reacquire_max_s"] == 1.0
    assert metrics["follow_false_confirm_count"] == 1
    assert metrics["follow_identity_pass"] is False
    assert metrics["moving_track_eligible"] is False


def test_mission_alert_matches_mover_at_observation_sim_time() -> None:
    from scripts.score_onboard_loop import _mission_metrics

    truth = {
        "targets": [
            {
                "target_id": "moving-car",
                "label": "vehicle car",
                "category_words": ["vehicle", "car"],
                "class": "vehicle",
                "pose": {"x_east_m": 0.0, "y_north_m": 0.0},
                "trajectory": {
                    "speed_mps": 1.0,
                    "waypoints": [[0.0, 0.0], [10.0, 0.0]],
                },
                "true_target": True,
            }
        ]
    }
    rows = [
        {
            "frame_sensor_stamp_ns": 5_000_000_000,
            "labels": {
                "onboard": {
                    "mission": {
                        "plan": {"skills": ["monitor"]},
                        "alerts": ["watch:alert:vehicle@0.0,5.0"],
                    }
                }
            },
        }
    ]

    metrics = _mission_metrics(rows, truth, radius_m=1.0)

    assert metrics["mission_alerts"][0]["time_s"] == 5.0
    assert metrics["mission_alerts"][0]["matches_query"] is True
    assert metrics["mission_alert_precision"] == 1.0


def test_mover_worlds_have_explicit_splits_and_sensor_rendered_trajectory(tmp_path) -> None:
    from multidetect.simulator.world_gen import generate_mover_worlds

    worlds = tmp_path / "worlds"
    scenarios = tmp_path / "scenarios"
    first = generate_mover_worlds(out_worlds=worlds, out_scenarios=scenarios)
    before = (worlds / "gen_mov_w0000.sdf").read_bytes()
    second = generate_mover_worlds(out_worlds=worlds, out_scenarios=scenarios)
    assert before == (worlds / "gen_mov_w0000.sdf").read_bytes()
    assert first == second
    assert {entry["split"] for entry in first["worlds"]} == {"train", "val", "test"}
    text = before.decode()
    assert '<model name="target_true_gen_mov_w0000"><static>true</static>' in text
    assert "gz-sim-label-system" in text
    assert "<actor" not in text
    assert "trajectory-follower" not in text


def test_verify_mover_postprocess_projects_truth_and_reads_recorded_labels(tmp_path) -> None:
    from PIL import Image

    from scripts.verify_mover_world import postprocess_episode

    episode = tmp_path / "episode-0000"
    (episode / "seg").mkdir(parents=True)
    (episode / "rgb").mkdir()
    cam = {"width": 64, "height": 48, "fx": 40.0, "fy": 40.0, "cx": 32.0, "cy": 24.0}
    pose = {
        "local_ned_m": [0.0, 0.0, -10.0],
        "attitude": {"roll_deg": 0.0, "pitch_deg": 0.0, "yaw_deg": 0.0},
    }
    seg = __import__("numpy").zeros((48, 64, 3), dtype="uint8")
    seg[20:28, 28:36, 2] = 1
    rgb = __import__("numpy").zeros((48, 64, 3), dtype="uint8")
    rgb[20:28, 28:36] = 255
    Image.fromarray(seg).save(episode / "seg/frame.png")
    Image.fromarray(rgb).save(episode / "rgb/frame.png")
    held = {
        "action": {"kind": "hold"},
        "obs_before": {
            "frame_sensor_stamp_ns": 0,
            "pose": pose,
            "attitude": {"roll_deg": 0.5, "pitch_deg": -0.5},
            "camera_info": cam,
            "segmentation_path": "seg/frame.png",
            "rgb_path": "rgb/frame.png",
        },
    }
    turning = {"action": {"kind": "turn"}, "obs_before": dict(held["obs_before"])}
    (episode / "steps.jsonl").write_text(json.dumps(held) + "\n" + json.dumps(turning) + "\n")
    truth = {
        "targets": [
            {
                "target_id": "t",
                "true_target": True,
                "pose": {"x_east_m": 0.0, "y_north_m": 5.0, "z_up_m": 0.0},
                "size_m": [2.0, 2.0, 2.0],
                "bottom_offset_m": -1.0,
            }
        ],
        "segmentation_labels": {"1": {"target_id": "t"}},
    }
    result = postprocess_episode(episode, truth)
    assert "median_centroid_error_px" in result
    # Only the frame taken while holding still is scored; the mid-turn one is skipped.
    assert result["skipped_maneuvering_frames"] == 1


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


def test_count_metrics_keeps_structured_report_before_terminal_mission_status() -> None:
    from scripts.score_onboard_loop import _count_metrics

    members = [
        {"pose": {"y_north_m": 0.0, "x_east_m": 0.0}},
        {"pose": {"y_north_m": 50.0, "x_east_m": 0.0}},
    ]
    # This is the recorded shape from a real episode: the count report is a
    # mapping, then health recovery/mission completion emits mission="done".
    rows = [
        {
            "labels": {
                "onboard": {
                    "mission": {
                        "counts": {"s1": 2},
                        "positions": ["0.5,0.0", "49.5,0.0"],
                    }
                }
            }
        },
        {"labels": {"onboard": {"mission": "done"}}},
    ]

    metrics = _count_metrics(rows, members, 2.0)
    assert metrics["reported_count"] == 2
    assert metrics["matched_count"] == 2
    assert metrics["false_findings"] == 0
    assert metrics["count_exact"] is True


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


def test_low_disk_score_checks_metadata_before_missing_or_truncated_steps(tmp_path: Path) -> None:
    from scripts.score_onboard_loop import score_episode

    episode = tmp_path / "aborted"
    episode.mkdir()
    (episode / "episode.json").write_text(
        json.dumps({"status": "aborted_low_disk", "failure": "disk floor"}),
        encoding="utf-8",
    )
    (episode / "steps.jsonl").write_text('{"step_index": 0}\n{"truncated', encoding="utf-8")
    result = score_episode(episode, {"targets": []})
    assert result["status"] == "aborted_low_disk"
    assert result["excluded_from_model_failure_metrics"] is True


def test_mover_trajectory_ids_are_integers_for_gazebo():
    # SDF requires <trajectory id> to be an integer; `id="move"` made Gazebo reject
    # the whole world (found by loading it in the real Gazebo Harmonic image).
    import xml.etree.ElementTree as ET
    from pathlib import Path

    for world in sorted(Path("simulation/gazebo/worlds").glob("gen_mov_w*.sdf")):
        for trajectory in ET.parse(world).getroot().iter("trajectory"):
            assert trajectory.get("id", "").isdigit(), world


def test_plural_alert_query_matches_singular_truth_words() -> None:
    from scripts.score_onboard_loop import _mission_metrics, _object_matches_query

    backpack = {"label": "backpack bag", "category_words": ["backpack", "bag"]}
    assert _object_matches_query(backpack, "backpacks")
    assert _object_matches_query({"label": "fire hydrant"}, "fire hydrants")
    assert _object_matches_query({"label": "bus"}, "buses")
    assert not _object_matches_query({"label": "postbox"}, "backpacks")
    truth = {
        "targets": [
            {**backpack, "pose": {"y_north_m": 0.0, "x_east_m": 0.0}, "true_target": True},
            {**backpack, "pose": {"y_north_m": 20.0, "x_east_m": 0.0}},
        ]
    }
    rows = [
        {
            "labels": {
                "onboard": {
                    "mission": {
                        "plan": {"skills": ["monitor", "return_home"]},
                        "alerts": ["work:alert:backpacks@0.2,0.0"],
                    }
                }
            }
        }
    ]
    metrics = _mission_metrics(rows, truth, 3.0)
    assert metrics["mission_alert_precision"] == 1.0
    assert metrics["mission_alert_eligible_count"] == 2
    assert metrics["mission_alert_recall"] == 0.5


def test_flight_safety_metrics_detect_ground_strike_and_return_distance() -> None:
    from scripts.score_onboard_loop import _flight_safety_metrics

    def row(n, e, alt):
        return {"obs_before": {"pose": {"local_ned_m": [n, e, -alt]}},
                "obs_after": {"pose": {"local_ned_m": [n, e, -alt]}},
                "labels": {"onboard": {"home_ne": [0.0, 0.0]}}}

    safe = _flight_safety_metrics(
        [row(0, 0, 30), row(2, 3, 29)],
        {"mission_completed": True},
        {"safety": {"min_relative_altitude_m": 10, "max_return_distance_m": 10}},
    )
    assert safe["ground_contact_detected"] is False
    assert safe["return_home_distance_m"] == 3.606
    assert safe["safety_pass"] is True

    crashed = _flight_safety_metrics(
        [row(0, 0, 30), row(4, 4, 0.2)],
        {"mission_completed": True},
        {"safety": {"min_relative_altitude_m": 10}},
    )
    assert crashed["ground_contact_detected"] is True
    assert crashed["safety_pass"] is False


def test_flight_safety_metrics_accepts_expect_safety_alias() -> None:
    from scripts.score_onboard_loop import _flight_safety_metrics

    row = {"obs_before": {"pose": {"local_ned_m": [0, 0, -20]}}}
    result = _flight_safety_metrics(
        [row], {"mission_completed": True}, {"expect": {"safety": {"min_relative_altitude_m": 5}}}
    )
    assert result["safety_min_altitude_pass"] is True
