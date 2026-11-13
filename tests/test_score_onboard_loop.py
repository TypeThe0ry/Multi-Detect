from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

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


def test_old_fixed_camera_score_projection_is_numerically_unchanged(tmp_path: Path) -> None:
    from multidetect.simulator.camera_projection import project_target_bbox
    from scripts.score_onboard_loop import _true_box, episode_camera_pitch_rad

    episode = tmp_path / "episode"
    episode.mkdir()
    (episode / "episode.json").write_text(
        json.dumps({"airframe_profile": {"id": "x500_mono_cam"}}), encoding="utf-8"
    )
    observation = {
        "camera_info": {"width": 1280, "height": 960, "fx": 540.0, "fy": 540.0, "cx": 640.0, "cy": 480.0},
        "pose": {"local_ned_m": [3.0, -2.0, -22.0]},
        "attitude": {"roll_deg": 2.0, "pitch_deg": -4.0, "yaw_deg": 35.0},
    }
    truth = {"targets": [{"true_target": True, "target_id": "t", "pose": {"x_east_m": 30.0, "y_north_m": 40.0, "z_up_m": 0.0}}]}
    actual, source = _true_box(episode, observation, truth)
    expected = project_target_bbox(
        target_ned=(40.0, 30.0, 0.0),
        pose=observation,
        camera_info=observation["camera_info"],
        image_size=(1280, 960),
        camera_pitch_rad=episode_camera_pitch_rad(episode),
    )
    assert source == "projection"
    assert actual == pytest.approx(expected)


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


def test_campaign_count_summary_ignores_non_count_mission_truth_members() -> None:
    from scripts.eval_onboard_loop_campaign import _count_summary

    complete = [
        {
            "mission_plan_skills": ["survey", "return_home"],
            "expected_count": 3,
            "reported_count": None,
            "count_exact": False,
        },
        {
            "mission_plan_skills": ["monitor", "return_home"],
            "expected_count": 3,
            "reported_count": None,
            "count_exact": False,
        },
        {
            "mission_plan_skills": ["count", "return_home"],
            "expected_count": 3,
            "reported_count": 3,
            "count_exact": True,
        },
    ]

    assert _count_summary(complete) == {
        "count_exact_rate": 1.0,
        "mean_abs_count_error": 0.0,
    }
    assert _count_summary(complete[:2]) == {
        "count_exact_rate": None,
        "mean_abs_count_error": None,
    }


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


def _target_free_fixture(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    fixture = json.loads((Path(__file__).parent / "fixtures/cities/lujiazui-target-free-score-20261109.json").read_text())
    (tmp_path / "episode.json").write_text(json.dumps(fixture["metadata"]))
    (tmp_path / "steps.jsonl").write_text("".join(json.dumps(row) + "\n" for row in fixture["rows"]))
    return fixture


def test_recorded_city_survey_scores_with_no_fabricated_identity_target(tmp_path: Path) -> None:
    from scripts.score_onboard_loop import score_episode
    from scripts.run_mission_suite import build_table

    fixture = _target_free_fixture(tmp_path)
    result = score_episode(tmp_path, fixture["truth"], mission_config=fixture["mission_config"])
    assert result["status"] == "complete" and result["steps"] == 31
    assert result["mission_plan_skills"] == ["survey", "return_home"]
    assert result["mission_completed"] is True and result["safety_pass"] is True
    assert result["mission_survey_coverage"] == {"s1": 1.0}
    assert result["target_scoring_applicable"] is False
    assert result["confirmed"] is False and result["success"] is False
    assert result["false_confirm"] is False and "position_error_m" not in result
    mission = {"id": "city", "world": "city", "expect": {
        "skills": ["survey", "return_home"], "min_survey_coverage": 0.7,
    }}
    result["world"] = "city"
    table = build_table([mission], {"city": {"episodes": [result]}})
    assert table["missions"][0]["expectations_passed"] is True
    result["mission_survey_coverage"] = {"s1": 0.69}
    assert build_table([mission], {"city": {"episodes": [result]}})["missions"][0]["expectations_passed"] is False


@pytest.mark.parametrize("skill", ["search", "count", "inspect", "patrol", "monitor", "follow", "land"])
def test_target_required_plans_still_reject_empty_truth(tmp_path: Path, skill: str) -> None:
    from scripts.score_onboard_loop import score_episode

    fixture = _target_free_fixture(tmp_path)
    fixture["rows"][0]["labels"]["onboard"]["mission"]["plan"] = {"skills": [skill, "return_home"]}
    (tmp_path / "steps.jsonl").write_text("".join(json.dumps(row) + "\n" for row in fixture["rows"]))
    with pytest.raises(StopIteration):
        score_episode(tmp_path, fixture["truth"], mission_config=fixture["mission_config"])


@pytest.mark.parametrize("extra", [{"mission": "count"}, {"expect": {"skills": ["inspect", "return_home"]}}, {"expect": {"not_found": True}}])
def test_target_required_config_cannot_hide_behind_a_geographic_plan(tmp_path: Path, extra: dict) -> None:
    from scripts.score_onboard_loop import score_episode

    fixture = _target_free_fixture(tmp_path)
    with pytest.raises(StopIteration):
        score_episode(tmp_path, fixture["truth"], mission_config={**fixture["mission_config"], **extra})


@pytest.mark.parametrize("state_before", ["survey", "returning"])
def test_target_free_unexpected_confirmation_remains_false_and_supervisor_is_not_skipped(tmp_path: Path, monkeypatch, state_before: str) -> None:
    from scripts import score_onboard_loop as scorer

    fixture = _target_free_fixture(tmp_path)
    fixture["rows"][-1]["labels"]["onboard"]["confirmed"] = {"position_estimate_ne": [0, 0]}
    fixture["rows"][-1]["labels"]["onboard"]["state_before"] = state_before
    (tmp_path / "steps.jsonl").write_text("".join(json.dumps(row) + "\n" for row in fixture["rows"]))
    result = scorer.score_episode(tmp_path, fixture["truth"], mission_config=fixture["mission_config"])
    assert result["confirmed"] is True and result["false_confirm"] is True and result["success"] is False
    monkeypatch.setattr(scorer, "_supervisor_evidence", lambda _: {"mission_plan_skills": ["inspect", "return_home"], "original_mission_completed": False})
    with pytest.raises(StopIteration):
        scorer.score_episode(tmp_path, fixture["truth"], mission_config={**fixture["mission_config"], "mission_package": {}})


def test_target_free_incomplete_return_is_not_relabelled_completed(tmp_path: Path) -> None:
    from scripts.score_onboard_loop import score_episode

    fixture = _target_free_fixture(tmp_path)
    fixture["rows"][-1]["labels"]["onboard"]["mission"]["history"][-1]["state"] = "failed"
    (tmp_path / "steps.jsonl").write_text("".join(json.dumps(row) + "\n" for row in fixture["rows"]))
    result = score_episode(tmp_path, fixture["truth"], mission_config=fixture["mission_config"])
    assert result["mission_completed"] is False and result["success"] is False


def test_targetful_geographic_episode_keeps_identical_legacy_metrics(tmp_path: Path) -> None:
    from scripts.score_onboard_loop import score_episode

    fixture = _target_free_fixture(tmp_path)
    geographic = score_episode(tmp_path, fixture["truth"], mission_config=fixture["mission_config"])
    targetful = score_episode(tmp_path, {"targets": [{"true_target": True, "target_id": "legacy-control"}]}, mission_config=fixture["mission_config"])
    geographic.pop("target_scoring_applicable")
    assert geographic == targetful


@pytest.mark.parametrize("truth", [{}, {"targets": None}, {"targets": [{}]}])
def test_target_free_plan_does_not_accept_missing_or_malformed_truth(tmp_path: Path, truth: dict) -> None:
    from scripts.score_onboard_loop import score_episode

    fixture = _target_free_fixture(tmp_path)
    with pytest.raises((KeyError, TypeError, StopIteration)):
        score_episode(tmp_path, truth, mission_config=fixture["mission_config"])


@pytest.mark.parametrize("identifier", ["comms-free-count", "comms-free-patrol-timeout"])
def test_real_merged_target_required_packages_cannot_use_geographic_labels(tmp_path: Path, identifier: str) -> None:
    from scripts.score_onboard_loop import score_episode
    from scripts.run_mission_suite import merge_config

    root = Path(__file__).resolve().parents[1]
    suite = json.loads((root / "configs/onboard/suite-main-r21idr3-r24plan.json").read_text())
    mission = next(row for row in suite["missions"] if row["id"] == identifier)
    config = merge_config(suite["defaults"], mission)
    assert config["mission"] == "from_text"  # Runtime package resolution supersedes this.
    fixture = _target_free_fixture(tmp_path)
    with pytest.raises(ValueError, match="package"):
        score_episode(tmp_path, fixture["truth"], mission_config=config)


@pytest.mark.parametrize("plan_change", [
    {"steps": [{"id": "hidden", "skill": "inspect", "params": {"target": "postbox"}, "on_success": "end"}]},
    {"unknown_declaration": True},
    {"skills": ["return_home"]},
])
def test_all_recorded_plan_declarations_must_be_geographic_and_consistent(tmp_path: Path, plan_change: dict) -> None:
    from scripts.score_onboard_loop import score_episode

    fixture = _target_free_fixture(tmp_path)
    # A later contradictory declaration cannot hide behind the first summary.
    fixture["rows"][-1]["labels"]["onboard"]["mission"]["plan"] = {
        "skills": ["survey", "return_home"], **plan_change,
    }
    (tmp_path / "steps.jsonl").write_text("".join(json.dumps(row) + "\n" for row in fixture["rows"]))
    with pytest.raises(ValueError):
        score_episode(tmp_path, fixture["truth"], mission_config=fixture["mission_config"])


def _geographic_package():
    import copy

    root = Path(__file__).resolve().parents[1]
    suite = json.loads((root / "configs/onboard/suite-main-r21idr3-r24plan.json").read_text())
    package = copy.deepcopy(next(row for row in suite["missions"] if row["id"] == "comms-free-count")["mission_package"])
    package["plan"]["goal"] = "Survey the site then return home"
    package["plan"]["steps"][0].update(skill="survey", params={"region": "zone:site", "overlap": 0.7})
    return package


def _write_geographic_package_log(episode: Path, package: dict) -> None:
    directory = episode.parent.parent / f"{episode.name}-onboard-frames" / "mission_log"
    directory.mkdir(parents=True)
    rows = [
        {"kind": "mission_started", "plan": package["plan"]},
        {"kind": "execution", "state": {"skill": "return_home"}, "action": {}},
        {"kind": "final_summary", "summary": {"action": "continue", "history": [
            {"skill": "survey", "state": "succeeded"},
            {"skill": "return_home", "state": "succeeded", "reason": "arrived"},
        ]}},
    ]
    (directory / "mission.jsonl").write_text("".join(json.dumps({"sequence": index, "mission_id": package["mission_id"], **row}) + "\n" for index, row in enumerate(rows, 1)))


@pytest.mark.parametrize("declared_mode", ["from_text", "package"])
def test_valid_geographic_package_requires_its_durable_plan_evidence(tmp_path: Path, declared_mode: str) -> None:
    from scripts.score_onboard_loop import score_episode

    episode = tmp_path / "world/episodes/episode"
    fixture = _target_free_fixture(episode)
    package = _geographic_package()
    config = {**fixture["mission_config"], "mission": declared_mode, "mission_package": package}
    with pytest.raises(ValueError, match="durable"):
        score_episode(episode, fixture["truth"], mission_config=config)
    _write_geographic_package_log(episode, package)
    result = score_episode(episode, fixture["truth"], mission_config=config)
    assert result["target_scoring_applicable"] is False and result["mission_completed"] is True
    assert result["mission_supervisor"]["onboard_log_exists"] is True


@pytest.mark.parametrize("change", ["missing_plan", "unknown_package_field", "target_param_on_survey"])
def test_geographic_package_missing_or_unknown_schema_stays_rejected(tmp_path: Path, change: str) -> None:
    from scripts.score_onboard_loop import score_episode

    fixture = _target_free_fixture(tmp_path)
    package = _geographic_package()
    if change == "missing_plan":
        package.pop("plan")
    elif change == "unknown_package_field":
        package["unknown"] = True
    else:
        package["plan"]["steps"][0]["params"]["query"] = "a person"
    with pytest.raises(ValueError):
        score_episode(tmp_path, fixture["truth"], mission_config={**fixture["mission_config"], "mission_package": package})


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


def _land_rows(
    *,
    mid_contact: bool = False,
    touchdown_vz: float = 0.7,
    landed_state: int | None = 1,
    land_label: bool = True,
):
    """Synthetic fly -> land episode: 20 m cruise, then land at ``touchdown_vz`` m/s."""

    def obs(alt, vz, t, state):
        flight = {} if state is None else {"landed_state": state}
        return {
            "pose": {"local_ned_m": [1.0, 2.0, -alt]},
            "velocity": [0.0, 0.0, vz],
            "pose_time_boot_ms": int(t * 1000),
            "flight_state": flight,
        }

    def row(before, after, skill):
        onboard = {"skill": skill, "home_ne": [0.0, 0.0]}
        return {"obs_before": before, "obs_after": after, "labels": {"onboard": onboard}}

    cruise_alt = 0.5 if mid_contact else 20.0
    rows = [
        row(obs(20.0, 0.0, 0, 2), obs(cruise_alt, 0.0, 1, 2), "area_search"),
        row(obs(cruise_alt, 0.0, 1, 2), obs(20.0, 0.0, 2, 2), "area_search"),
    ]
    skill = "land" if land_label else "area_search"
    rows.append(row(obs(20.0, 0.0, 2, 2), obs(19.9, 0.0, 3, 4), skill))
    t2 = 3 + 17.9 / touchdown_vz
    rows.append(row(obs(19.9, touchdown_vz, 3, 4), obs(2.0, touchdown_vz, t2, 4), skill))
    final_alt = 0.0
    t3 = t2 + 2.0 / touchdown_vz
    rows.append(row(obs(2.0, touchdown_vz, t2, 4), obs(final_alt, touchdown_vz, t3, 4), skill))
    rows.append(row(obs(final_alt, 0.0, t3 + 1, 4), obs(final_alt, 0.0, t3 + 2, landed_state), skill))
    return rows


_LAND_METRICS = {
    "mission_completed": True,
    "mission_plan_skills": ["area_search", "land"],
    "mission_step_outcomes": [
        {"step": "s1", "skill": "area_search", "state": "succeeded", "reason": "confirmed"},
        {"step": "s9", "skill": "land", "state": "succeeded", "reason": "landed"},
    ],
}


def test_terminal_land_good_landing_passes() -> None:
    from scripts.score_onboard_loop import _flight_safety_metrics

    result = _flight_safety_metrics(_land_rows(), _LAND_METRICS, {})
    assert result["terminal_landing_assessed"] is True
    assert result["ground_contact_detected"] is False
    assert result["landing_pass"] is True
    assert result["safety_pass"] is True
    assert result["landing_touchdown_max_descent_mps"] <= 0.75


def test_terminal_land_mid_mission_ground_contact_fails() -> None:
    from scripts.score_onboard_loop import _flight_safety_metrics

    result = _flight_safety_metrics(_land_rows(mid_contact=True), _LAND_METRICS, {})
    assert result["ground_contact_detected"] is True
    assert result["safety_pass"] is False


def test_terminal_land_hard_touchdown_fails() -> None:
    from scripts.score_onboard_loop import _flight_safety_metrics

    result = _flight_safety_metrics(_land_rows(touchdown_vz=3.0), _LAND_METRICS, {})
    assert result["landing_pass"] is False
    assert result["safety_pass"] is False
    # The limit is configurable through the existing safety config.
    relaxed = _flight_safety_metrics(
        _land_rows(touchdown_vz=3.0), _LAND_METRICS, {"safety": {"touchdown_max_descent_mps": 4.0}}
    )
    assert relaxed["safety_pass"] is True


def test_terminal_land_without_on_ground_confirmation_fails() -> None:
    from scripts.score_onboard_loop import _flight_safety_metrics

    for state in (None, 4, 2):
        result = _flight_safety_metrics(_land_rows(landed_state=state), _LAND_METRICS, {})
        assert result["landing_on_ground_confirmed"] is False
        assert result["safety_pass"] is False


def test_land_step_not_completed_or_not_last_keeps_legacy_rule() -> None:
    from scripts.score_onboard_loop import _flight_safety_metrics

    failed = dict(_LAND_METRICS, mission_completed=False)
    assert _flight_safety_metrics(_land_rows(), failed, {})["safety_pass"] is False
    not_last = dict(_LAND_METRICS, mission_plan_skills=["land", "return_home"])
    result = _flight_safety_metrics(_land_rows(), not_last, {})
    assert "terminal_landing_assessed" not in result
    assert result["ground_contact_detected"] is True and result["safety_pass"] is False
    # No row labelled as the land skill: cannot separate touchdown, legacy rule.
    unlabeled = _flight_safety_metrics(_land_rows(land_label=False), _LAND_METRICS, {})
    assert unlabeled["safety_pass"] is False


def test_non_land_mission_low_altitude_unchanged() -> None:
    from scripts.score_onboard_loop import _flight_safety_metrics

    metrics = {
        "mission_completed": True,
        "mission_plan_skills": ["area_search", "return_home"],
        "mission_step_outcomes": [{"skill": "return_home", "state": "succeeded"}],
    }
    result = _flight_safety_metrics(_land_rows(), metrics, {})
    assert "terminal_landing_assessed" not in result
    assert result["ground_contact_detected"] is True
    assert result["safety_pass"] is False
